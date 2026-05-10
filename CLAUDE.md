# Agent Guidance — NEXTPLAY (FastAPI)

> This file briefs an AI agent (Claude / Cursor / similar) on the
> conventions, contracts, and pitfalls of this codebase. It assumes
> familiarity with FastAPI + SQLAlchemy 2.0 async + general Python.
> If you're a human, read [README.md](README.md) first — this file is
> the *agent-facing* delta.

---

## Project at a glance

**NEXTPLAY** is a basketball coaching AI assistant. A coach signs up,
sets up their team's roster + style, then chats with a panel of five
AI specialist personas (GM / Scout / Analytics / Tactics / Training)
that each have access to the team's private data plus shared
basketball knowledge.

The product wraps:
- A streaming SSE chat surface (fast 2-5s mode + full multi-step
  CrewAI mode 30-60s)
- Tenant-scoped data (every coach + every team is isolated)
- Web research (Serper search + Jina Reader fetch) for opponent scouting
- ChromaDB RAG over a shared basketball knowledge base
- Post-chat memory extraction (the agent learns the coach's style /
  decisions over time, scoped per-team)
- Vision pipeline (GPT-4o Vision describes uploaded images, then a
  specialist analyzes the description)
- Web push, email (verification / reset / receipts), OAuth (Google +
  Facebook + Apple), avatar uploads, S3-backed scouting videos with
  multipart presign, telestrator clip annotations, plays, notebook,
  admin panel — the full B2C / B2B dual-mode product

This codebase is the FastAPI port of an earlier Flask app
(`v1.0-flask`). **Behavioral parity with v1 is the primary contract.**

---

## The One Rule

You are not building a new app. You are migrating + maintaining a
working app on a new framework while preserving its exact behavior.

Before writing code, answer:
1. What does the v1 Flask code do here?
2. Will my code do *exactly* the same thing — same edge cases, same
   security model, same multi-tenancy, same response shape, same
   error text, same status codes?

If you cannot answer "yes" to (2), STOP and ask the user.

The v1 Flask repo lives at `C:\Ohad\ohad\Ai Dev\CrewAI_agents\CrewAI\basketball_coach_ai`
and is the source of truth for any behavior question.

---

## Architecture invariants — DO NOT BREAK

These are load-bearing rules. Each one is here because v1 fielded a
real bug or scaling pain that this rule prevents.

1. **Multi-tenancy is the prime directive.** Every CrewAI tool + every
   DB query takes `user_id` + `team_id`. Tools use **factory closures**
   ([src/crew/tools.py](src/crew/tools.py)) — `(user_id, team_id)` are
   captured at construction time, never exposed in the OpenAI function
   schema. The LLM literally cannot ask for someone else's data.
   Repository pattern: see `TeamScopedRepository` — `list_for_user_team(None, None)`
   returns `[]` (defensive against accidentally-unscoped queries).

2. **Streaming chat is SSE, byte-for-byte v1 format**:
   ```
   data: {"t":"chunk","c":"..."}\n\n
   data: {"t":"tool","name":"research_external_team","status":"start"}\n\n
   data: {"t":"done"}\n\n
   data: {"t":"error","message":"..."}\n\n
   ```
   Don't switch to JSON polling. Don't change the wire format.

3. **Two chat modes — fast and full.**
   - Fast: direct OpenAI tool-loop in [src/services/chat_service.py:_run_with_tools](src/services/chat_service.py).
     2-5s, 1-2 round trips. Used 90% of the time.
   - Full: CrewAI multi-step orchestration in [src/crew/manager.py:run_full_chat](src/crew/manager.py).
     30-60s, costs more. Used when the question needs sustained
     reasoning + tool use (scout an opponent, build a season plan).
   - **Routing is 3-layer in this order** ([src/crew/routing.py](src/crew/routing.py)):
     deterministic regex shortcuts → semantic match against own-team
     facts → LLM classifier fallback. Preserve all three.

4. **Memory writes are async.** Post-chat memory extraction runs in
   `BackgroundTasks` ([src/services/chat_service.py:schedule_memory_extraction](src/services/chat_service.py)),
   never blocks the response. The background task opens its own
   `AsyncSessionLocal()` because the request session is closed by then.

5. **Smart memory scoping rules — verbatim from v1, do not change**:
   - `style` / `preference` / `philosophy` → `team_id = NULL`
     (cross-team — the coach has these regardless of which team)
   - `insight` / `decision` / `pattern` / `fact` → `team_id =
     active_team_id` (team-specific)

   If you change this, you break the product.

6. **Auth is dual-mode.** Cookie (browser) **and** `Authorization:
   Bearer` (mobile / API). The `get_current_user` dependency tries
   cookie first, then header. Both must work.

7. **CSRF rules are subtle** ([src/middleware/csrf.py](src/middleware/csrf.py)):
   state-changing `/api/*` requires `X-Requested-With` OR `Content-Type:
   application/json`. Exempt: `/api/auth/*`, OAuth callbacks,
   Bearer-tokened requests, `/api/internal/*` (cron with shared secret).

8. **Trial / purge state machine.** `@check_subscription` flips
   expired plans + sets `data_purge_at = NOW + 30d`. Next login past
   `data_purge_at` runs `purge_user_data`. **Club members
   (`club_id IS NOT NULL`) are exempt.** Test this — it's silently
   destructive if it regresses.

9. **OpenAI cost tracking — every call.** Direct calls go through
   `log_response()` ([src/crew/llm.py](src/crew/llm.py)). Streaming uses
   `stream_options={"include_usage": True}`. CrewAI internals captured
   via `crew.usage_metrics` after `kickoff()`. Untracked calls = silent
   billing leaks.

10. **Research cache is tenant-keyed.** Cache key is
    `(user_id, team_id, query, level_hint, url_hint, hour_bucket)`.
    v1 had a process-global cache that leaked Coach A's results to
    Coach B for the same query in the same hour — **fixed in
    [src/research/cache.py](src/research/cache.py); do not regress.**

11. **No backwards-compat shims.** Delete old code, don't `_legacy_`
    it. Don't keep dead `# removed for X` comments. Tag history is in
    git.

12. **English-first UI.** No Hebrew/RTL changes during the migration.
    Code comments + docstrings are English. Hebrew strings are OK in
    `frontend/templates/` ONLY where v1 had them.

13. **Multi-org tenancy (Phase 0 Enterprise).** `/org/*` and `/admin/*`
    use independent Starlette session keys (`org_user_id` / `org_active_org_id`
    / `org_active_role` vs `admin_email`); Coach App keeps JWT. Three
    independent auth contexts can coexist on a single request — never
    `request.session.clear()` in any logout, only pop the namespace's keys.
    Cross-org access (path mismatch, role mismatch, missing membership)
    raises `NotFoundError` (404), **never** `ForbiddenError` (403). Three
    layers of defense: `OrgContextMiddleware` → `OrgScopedRepository` →
    PostgreSQL RLS via `set_config('app.current_org_id', ...)` in
    [src/core/database.py:get_db](src/core/database.py). RLS is a no-op
    on SQLite (tests + CI alembic-smoke); production verification is the
    [tools/verify_rls.py](tools/verify_rls.py) smoke script.

---

## Async patterns

This codebase is async end-to-end. Three rules:

1. **Sync libs are wrapped in `asyncio.to_thread`** when there's no
   async equivalent. Examples:
   - CrewAI: `await asyncio.to_thread(crew.kickoff, inputs=inputs)`
   - PIL avatar resize: `await asyncio.to_thread(_process_avatar_sync, ...)`
   - PyMuPDF / openpyxl / pandas: same pattern in
     [src/services/file_processor.py](src/services/file_processor.py)
   - ChromaDB queries: same in [src/crew/knowledge_base.py](src/crew/knowledge_base.py)

2. **DB sessions yield from `get_db`** ([src/core/database.py](src/core/database.py)),
   which **commits on success, rolls back on exception**. Do not call
   `session.commit()` inside route handlers — the dependency does it.
   Exception: `BackgroundTasks` open their own `AsyncSessionLocal()`
   and must commit themselves.

3. **`selectinload()` for one-to-many relationships** that you'll
   touch on a closed session. SQLAlchemy 2.0 lazy-loads on attribute
   access; on a closed session that raises `MissingGreenlet`. The list
   of relationships that need `selectinload` is in
   [MIGRATION_TODO.md](MIGRATION_TODO.md).

---

## Where things live

```
src/
├── api/                        # Routers — one per domain
│   ├── pages.py                # 17 HTML page routes (Phase 8)
│   ├── admin_pages.py          # 13 admin HTML routes (session-auth)
│   ├── chat.py                 # /api/chat, /api/chat-stream, /api/chat-upload
│   ├── auth.py + email_auth.py + oauth.py    # JWT, email verify, 3 OAuth
│   ├── admin.py + admin_tasks.py + admin_emails.py    # Admin JSON API
│   ├── coach.py teams.py players.py ...      # Domain CRUD
│   ├── composite.py            # Aggregator endpoints (/api/me, /api/dashboard)
│   └── deps/                   # FastAPI dependencies (auth, db, csrf)
├── auth/                       # admin_auth, jwt_service, oauth_service, purge_service
├── core/                       # config, database (engine + get_db), exceptions, security
├── crew/
│   ├── manager.py              # CrewAI orchestrator (full mode)
│   ├── routing.py              # 3-layer router
│   ├── tools.py                # team_database / knowledge_base / research factories
│   ├── agents.py + prompts.py  # 5 specialist personas
│   ├── knowledge_base.py       # ChromaDB persistent client + reranker
│   └── llm.py                  # OpenAI client + log_response wrapper
├── frontend.py                 # Jinja2Templates + Flask compat shims (url_for, g, request.args)
├── middleware/                 # CSRFMiddleware, SecurityHeadersMiddleware, RateLimitMiddleware
├── models/                     # SQLAlchemy 2.0 ORM (~30 tables, 18 module files)
├── repositories/               # Async data access; TeamScopedRepository base
├── research/                   # 8-stage research pipeline (Plan→Search→Triage→Fetch→Extract→Verify→Synthesize)
├── schemas/                    # Pydantic v2 request/response shapes
├── services/                   # Business logic
│   ├── chat_service.py         # send_message + stream_message + send_chat_with_uploads
│   ├── memory_extractor.py     # post-chat memory write (gpt-4o-mini)
│   ├── vision.py               # 2-stage image pipeline
│   ├── file_processor.py       # PDF / Excel / CSV → text
│   ├── kb_ingest.py            # KB document chunking + embedding
│   ├── push_service.py         # pywebpush + gate stack + cron
│   ├── email.py + email_service.py    # Resend + console mode
│   ├── s3.py                   # aioboto3 wrapper
│   └── upload_service.py       # Local file storage + magic-byte validation
└── main.py                     # FastAPI app + middleware order + router includes

frontend/
├── templates/                  # 27 Jinja2 templates (verbatim from v1)
└── static/                     # 26 JS, 18 CSS, manifest, sw.js / upload-sw.js

alembic/                        # ONE baseline (34b9481e6509) + future migrations
knowledge_base/
├── chroma_store/               # ChromaDB persistent (gitignored)
└── documents/                  # Source PDFs / MDs the KB indexes
scripts/
├── build.py                    # CSS / JS minifier (no npm)
├── ingest_kb.py                # KB ingestion CLI
└── set_admin_password.py       # bcrypt hash + .env update
tests/                          # 633 pytest-asyncio tests
data/                           # Local SQLite + uploads (gitignored)
```

Top-level: `Procfile`, `railway.json`, `pyproject.toml`,
`requirements.txt`, `runtime.txt`, `.env.example`, `MIGRATION_TODO.md`,
`README.md`, `CLAUDE.md` (this file).

---

## Common workflows

### Adding a new domain endpoint

1. Pydantic schemas in [src/schemas/](src/schemas/) — Request +
   Response. Use `model_config = ConfigDict(from_attributes=True)`.
2. Repository method in [src/repositories/](src/repositories/) if data
   access is non-trivial. Use `TeamScopedRepository` if the table has
   `user_id` + `team_id`.
3. Router in [src/api/](src/api/) — usually just adding a function to
   an existing module. Use `Depends(get_current_user)` + `Depends(get_db)`.
4. Tests: at least one happy-path + one cross-tenant-rejection test.
   See [tests/api/test_teams.py](tests/api/test_teams.py) for the pattern.

### Adding a new CrewAI tool

1. Factory function in [src/crew/tools.py](src/crew/tools.py) returning
   a `Tool` dataclass. Capture `user_id` + `team_id` in the closure;
   keep them out of the JSON schema.
2. Add the tool key to `_AGENT_TOOL_MAP` for whichever specialists
   should have it.
3. Test: factory test + behavior test under `tests/crew/test_tools.py`.

### Adding a model + migration

1. Model in [src/models/](src/models/). Inherit from `Base` +
   `TimestampMixin` (only if v1 had timestamps on this table).
2. Import the model in [src/models/__init__.py](src/models/__init__.py)
   so Alembic sees it.
3. Generate: `alembic revision --autogenerate -m "add_<table>"`.
4. **Read the generated migration** — autogenerate is not perfect,
   especially for JSON columns (use `JSONText` from
   [src/core/database.py](src/core/database.py)) and indexes.
5. `alembic upgrade head` against your local DB.
6. Test: pytest fixture creates / drops via `Base.metadata.create_all`;
   the integration runs against the migration in CI.

### Touching streaming chat

The streaming path supports tools + tool-call deltas. See
[src/services/chat_service.py:stream_message](src/services/chat_service.py).
Each iteration:
- Streams `delta.content` as `t:chunk` events
- Accumulates `delta.tool_calls` deltas (keyed by `index`)
- After stream: if any tool calls → execute them, append assistant +
  tool messages, loop. Cap at `_TOOL_LOOP_MAX_ITERS = 3`.

The frontend silently ignores unknown events, so you can add new
event types (`t:tool`, `t:status`, etc.) without breaking it.

---

## Testing

- `pytest -q` — full suite (~2 min)
- `pytest tests/api/test_chat.py -v` — one module
- `pytest -k "tenant"` — only cross-tenant tests
- `pytest --cov=src --cov-report=html` — with coverage

Patterns:
- `api_client` fixture: `httpx.AsyncClient` already wired with
  rate-limit disabled, DB rollback after each test
- `e2e_client` fixture: same but with full auth machinery
- `fake_openai` fixture: monkey-patches `chat_service.get_client` +
  `memory_extractor.get_client` + `web_researcher.get_client` so no
  real OpenAI calls happen in tests
- Cross-tenant tests: seed user A's data, log in as user B, expect 404
  / 403 / empty list (whichever is documented)

CI runs ruff + bandit (`--skip B101,B608,B113`) + alembic upgrade +
pytest + asset build on every push.

---

## Pitfalls / gotchas

- **Windows + `--reload`**: `watchfiles` is unreliable for in-place
  edits to already-loaded modules. If a code change isn't reflected
  after a `Get-Process python | Stop-Process -Force` + restart, that's
  why. Always do a full restart on Windows after invasive edits.
- **Port 5060 / 5061**: Chrome blocks them as `ERR_UNSAFE_PORT` (SIP).
  Use 8080 / 5050 / 5070 / 3000.
- **SQLite vs Postgres**: tests use SQLite via `aiosqlite`. Production
  uses Postgres via `asyncpg`. JSON columns use the `JSONText`
  TypeDecorator so both engines work. Some queries (`func.now()`,
  array operations) need engine-specific handling — see
  [src/core/database.py](src/core/database.py).
- **`flush` vs `commit`**: route handlers should `await db.flush()` to
  populate `id` after `db.add(...)`. Don't `commit` — the dependency
  does that on success.
- **CSRF + Form data**: multipart / form-urlencoded POSTs to `/api/*`
  must include `X-Requested-With: XMLHttpRequest` (the SPA already
  does this in `auth.js`).
- **`Conversation.role`**: `user` / `assistant` / `system` only. The
  loader in `_load_history` coerces specialty roles back to
  `assistant` — anything else gets rejected by the OpenAI API.
- **OpenAI model strings**: the chat service + memory extractor +
  vision pipeline + crew manager all reference the same model
  constants. If you bump a model, search across all four.

---

## Reference

- **Migration plan**: `~/.claude/plans/nextplay-master-prompt-compressed-fox.md`
- **Deferred items**: [MIGRATION_TODO.md](MIGRATION_TODO.md)
- **Schema baseline**: [schema_baseline.md](schema_baseline.md)
- **v1 Flask source**: `C:\Ohad\ohad\Ai Dev\CrewAI_agents\CrewAI\basketball_coach_ai`
