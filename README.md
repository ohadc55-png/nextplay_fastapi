# NEXTPLAY

> AI basketball coaching assistant. A coach signs up, sets up their
> team, and chats with a panel of five specialist AI personas (GM,
> Scout, Analytics, Tactics, Training) that have access to the team's
> private data plus a shared basketball knowledge base.

This is the **FastAPI** port of the original Flask app (`v1.0-flask`).
Behavioral parity with v1 is preserved end-to-end — same API surface,
same multi-tenancy, same response shapes, same auth model.

---

## What the app does

NEXTPLAY is a B2C / B2B dual-mode product targeting basketball coaches:

- **Real-time chat with five specialist agents.** Each persona has a
  scoped tool kit:
  - **Brad Binn (GM)** — roster, lineups, team building. Gets
    `query_team_database` + `search_knowledge_base`.
  - **Jack Hunter (Scout)** — opposition scouting, opponent analysis.
    Adds `research_external_team` (Serper search + Jina Reader fetch).
  - **Nexus (Analytics)** — stats, metrics, data analysis. Same tools
    as Scout.
  - **Vance (Tactics)** — game plans, plays, strategy. Same tools.
  - **Williams (Assistant Coach)** — practice plans, drills,
    development. Same tools.

- **Two chat modes** — fast (direct OpenAI tool-loop, 2-5s) for
  quick questions, full (CrewAI multi-step orchestration, 30-60s) for
  deep work like full opponent reports or season plans.

- **Multi-tenancy from the ground up.** Each coach can manage multiple
  teams; every DB query + every agent tool is scoped by `(user_id,
  team_id)`. Tools use factory closures so the LLM cannot inject
  another coach's IDs.

- **Memory.** Post-chat, a background task extracts insights /
  decisions / facts and stores them with smart team scoping (style and
  preferences are cross-team; insights and facts are team-specific).
  Future chat turns retrieve relevant memories via cosine similarity
  on stored embeddings.

- **RAG over a basketball knowledge base.** ChromaDB persistent store
  indexed with OpenAI's `text-embedding-3-small`, optionally reranked
  by a sentence-transformers cross-encoder. Drills, plays, scouting
  frameworks — shared across all coaches (it's a domain library, not
  per-tenant).

- **Web research pipeline (8 stages).** When a coach asks Scout about
  an opponent, the agent calls `research_external_team`, which runs
  Plan → Search (Serper) → Triage → Fetch (Jina Reader, 4-layer
  fallback) → Extract (per-page LLM) → Verify (cross-source) →
  Synthesize (structured scout report). Cache keyed by
  `(user_id, team_id, query, level_hint, url_hint, hour_bucket)` so
  results never leak across coaches.

- **Vision.** Coach uploads an image (game scene, play diagram, shot
  chart, stat sheet). Stage 1 (GPT-4o Vision) describes it
  structurally. Stage 2 hands the description to the right specialist
  for analysis.

- **Files.** PDF / Excel / CSV uploaded in chat are extracted to text
  by PyMuPDF / openpyxl / pandas (sync libraries wrapped in
  `asyncio.to_thread`).

- **Plays + Notebook + Scouting Videos.** Three coach-facing surfaces
  beyond chat. Plays editor, journal entries with attendance, S3-
  backed video uploads with telestrator clip annotations + multipart
  presigned upload + 4-layer Jina/ScrapeWebsiteTool/Playwright
  fallback for fetcher robustness.

- **Auth.** Email + password (bcrypt, JWT HS256, refresh rotation),
  OAuth (Google + Facebook + Apple via Authlib), email verification,
  password reset. Cookie + Bearer dual-mode (browser + mobile).

- **Admin panel.** 13 HTML pages with session-based auth (separate
  from coach auth). Dashboard, users, API costs, feedback, sales
  inquiries, email log, tasks (with subtasks + comments), feature
  usage, geography, user activity, research sources, email lists,
  email compose.

- **Push, email, OAuth, S3, trial / purge state machine, custom
  rate-limiter, CSRF, security headers, Sentry, OpenAPI docs at
  `/docs`** — production-grade plumbing across the board.

---

## Stack at a glance

| Layer | Technology |
|-------|-----------|
| Web framework | FastAPI 0.115 |
| Server | uvicorn (Railway: 2 workers, 300s keep-alive for full-mode chats) |
| Templates | Jinja2 (serves verbatim v1 templates via shims) |
| Validation | Pydantic v2 |
| ORM | SQLAlchemy 2.0 async |
| DB drivers | `asyncpg` (prod Postgres) + `aiosqlite` (dev SQLite) |
| Migrations | Alembic |
| Auth | python-jose (JWT) + bcrypt + Authlib (OAuth) |
| Storage | aioboto3 (S3) + Pillow (avatar resize / WebP) |
| AI | OpenAI Python SDK + CrewAI 1.10 |
| Embeddings | OpenAI `text-embedding-3-small` |
| Vector store | ChromaDB persistent |
| Reranker | sentence-transformers `ms-marco-MiniLM-L-12-v2` (optional) |
| Research | Serper (search) + Jina Reader (fetch) + Playwright (fallback) |
| Email | Resend |
| Push | pywebpush (VAPID) |
| File extraction | PyMuPDF + openpyxl + pandas |
| Observability | Sentry FastAPI integration |
| Frontend | Vanilla JS, no bundler (`scripts/build.py` minifies CSS / JS) |
| Testing | pytest + pytest-asyncio + httpx |
| CI | GitHub Actions: ruff + bandit + alembic + pytest |
| Hosting | Railway (Nixpacks) |

---

## Architecture overview

```
                             ┌──────────────┐
                             │   Browser    │
                             │  (vanilla    │
                             │   JS SPA)    │
                             └──────┬───────┘
                                    │ cookie / Bearer
                                    ▼
              ┌─────────────────────────────────────┐
              │         FastAPI app (uvicorn)       │
              │                                     │
              │ Middleware (outer → inner):         │
              │  CORS → SecurityHeaders → CSRF      │
              │   → RateLimit → SessionMiddleware   │
              │                                     │
              │ Routers:                            │
              │  /api/auth/*  /api/chat-stream      │
              │  /api/teams/* /api/players/* ...    │
              │  /admin/*     /admin/api/*          │
              │  /            /chat ...  (HTML)     │
              └──┬─────────┬─────────┬─────────┬────┘
                 │         │         │         │
                 ▼         ▼         ▼         ▼
          ┌──────────┐ ┌──────┐ ┌────────┐ ┌──────────┐
          │ Postgres │ │ S3   │ │ OpenAI │ │ ChromaDB │
          │ (asyncpg)│ │      │ │  API   │ │ (RAG)    │
          └──────────┘ └──────┘ └────┬───┘ └──────────┘
                                     │
                                     ▼
                            ┌────────────────┐
                            │    CrewAI      │
                            │ (full mode) +  │
                            │  Serper / Jina │
                            │   research     │
                            └────────────────┘
```

**Request flow for streaming chat (`POST /api/chat-stream`)**:

1. Auth — `get_current_user` reads cookie or `Authorization: Bearer`.
2. CSRF — `X-Requested-With: XMLHttpRequest` or JSON content-type
   required (Bearer-tokened requests are exempt).
3. Save the user message to `conversations`.
4. Load last 12 turns of history.
5. Build team context (roster + profile + style).
6. Route — 3-layer (deterministic regex → semantic → LLM
   classifier) picks the agent.
7. Build the persona system prompt + per-agent tools.
8. Stream from OpenAI. Each iteration:
   - Stream `delta.content` as `t:chunk` SSE events
   - Accumulate `delta.tool_calls` deltas
   - On tool calls: emit `t:tool` event, execute tool (e.g.,
     `research_external_team` runs the 8-stage pipeline), append the
     tool result to the conversation, loop
9. Save the assistant response to `conversations`.
10. Schedule a background task to extract memories (gpt-4o-mini parses
    the turn, writes rows with smart team scoping).
11. Send `t:done` and close the stream.

---

## Quick start (local dev)

```powershell
# 1. Clone + venv
git clone <repo>
cd nextplay-fastapi
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Configure
copy .env.example .env
# At minimum, fill in:
#   OPENAI_API_KEY        — for chat / agents / KB embeddings
#   JWT_SECRET_KEY        — 32+ chars random
#   SESSION_SECRET_KEY    — for OAuth state cookies
#   ADMIN_PASSWORD_HASH   — bcrypt hash; or run scripts/set_admin_password.py
# Leave DATABASE_URL empty to use the local SQLite fallback at ./data/coach.db.

# 3. Apply migrations (creates ~30 tables in Postgres or SQLite)
alembic upgrade head

# 4. (Optional) Ingest the basketball knowledge base
python scripts/ingest_kb.py

# 5. Run the dev server (port 5050; --reload watches src/ + frontend/templates/)
python -m uvicorn src.main:app --host 127.0.0.1 --port 5050 --reload --reload-dir src --reload-dir frontend/templates

# 6. Verify
curl http://localhost:5050/healthz
# -> {"status":"ok","db":"connected","chroma":"empty","version":"2.0.0-alpha"}

# 7. Open the SPA
# http://localhost:5050/main      (landing)
# http://localhost:5050/login     (login form)
# http://localhost:5050/admin/login (admin login)
# http://localhost:5050/docs      (Swagger UI for the API)
```

> Avoid port 5060 / 5061 — Chrome blocks them as `ERR_UNSAFE_PORT`
> (SIP). 8080 / 5050 / 5070 / 3000 all work.

### Resetting the admin password

```powershell
python scripts/set_admin_password.py
# Prompts for new password (hidden input), generates bcrypt hash with
# cost 12, updates ADMIN_PASSWORD_HASH= in .env. Restart uvicorn.
```

---

## Running tests

```powershell
# Full suite (~2 min)
pytest -q

# One module
pytest tests/api/test_chat.py -v

# Cross-tenant tests only
pytest -k "tenant"

# With coverage
pytest --cov=src --cov-report=html
```

633 tests, 1 xfailed (documented `aiosqlite + autoflush=False` quirk
that resolves under asyncpg).

CI (`.github/workflows/ci.yml`) runs ruff + bandit + alembic upgrade +
pytest + asset build on every push to `main` / `dev` and on every PR.

---

## Project layout

```
nextplay_fastapi/
├── src/
│   ├── api/                # FastAPI routers (one per domain)
│   │   ├── pages.py        # 17 HTML page routes
│   │   ├── admin_pages.py  # 13 admin HTML routes
│   │   ├── chat.py         # /api/chat, /api/chat-stream, /api/chat-upload
│   │   ├── auth.py         # JWT + email-password
│   │   ├── email_auth.py   # Verify / reset / change-password
│   │   ├── oauth.py        # Google + Facebook + Apple
│   │   ├── admin.py        # /admin/login + /admin/api/*
│   │   ├── admin_tasks.py  # Admin todo CRUD
│   │   ├── admin_emails.py # Email lists + campaigns
│   │   ├── coach.py teams.py players.py ...
│   │   ├── composite.py    # Aggregator endpoints
│   │   └── deps/           # FastAPI dependencies
│   ├── auth/               # JWT + OAuth + admin auth + purge state machine
│   ├── core/               # config, database, exceptions, security
│   ├── crew/
│   │   ├── manager.py      # CrewAI orchestrator (full mode)
│   │   ├── routing.py      # 3-layer router
│   │   ├── tools.py        # Tool factory closures (team_db / kb / research)
│   │   ├── agents.py       # 5 personas + composition
│   │   ├── prompts.py      # Verbatim v1 prompts
│   │   ├── knowledge_base.py # ChromaDB wrapper + reranker
│   │   └── llm.py          # OpenAI client + log_response
│   ├── frontend.py         # Jinja2Templates + Flask compat shims
│   ├── middleware/         # CSRF, security headers, rate limiter
│   ├── models/             # SQLAlchemy ORM (~30 tables, 18 module files)
│   ├── repositories/       # Async data access (TeamScopedRepository base)
│   ├── research/           # 8-stage research pipeline
│   ├── schemas/            # Pydantic request/response shapes
│   ├── services/
│   │   ├── chat_service.py     # send_message + stream_message + uploads
│   │   ├── memory_extractor.py # Post-chat memory writes
│   │   ├── vision.py           # 2-stage image pipeline
│   │   ├── file_processor.py   # PDF / Excel / CSV → text
│   │   ├── kb_ingest.py        # KB chunking + embedding
│   │   ├── push_service.py     # pywebpush + cron
│   │   ├── email.py            # Resend wrapper
│   │   ├── s3.py               # aioboto3 wrapper
│   │   └── upload_service.py   # Local file storage
│   └── main.py             # FastAPI app + middleware order + router includes
│
├── frontend/
│   ├── templates/          # 27 Jinja2 templates (verbatim from v1)
│   └── static/             # 26 JS, 18 CSS, manifest, sw.js / upload-sw.js
│
├── alembic/                # Schema migrations (one baseline + future)
├── knowledge_base/
│   ├── chroma_store/       # ChromaDB persistent (gitignored)
│   └── documents/          # Source PDFs / MDs to index
├── scripts/
│   ├── build.py            # CSS / JS minifier (no npm)
│   ├── ingest_kb.py        # KB document ingestion
│   └── set_admin_password.py
├── tests/                  # 633 pytest-asyncio tests
├── data/                   # Local SQLite + uploads (gitignored)
│
├── .env.example            # Documented env var template
├── .github/workflows/      # CI
├── Procfile                # Railway: alembic + uvicorn
├── pyproject.toml          # ruff + pytest config
├── railway.json            # Nixpacks + healthcheck
├── requirements.txt        # Pinned dependencies
├── runtime.txt             # python-3.11
├── README.md               # This file
├── CLAUDE.md               # Agent guidance
├── MIGRATION_TODO.md       # Deferred items
├── schema_baseline.md      # Schema reference
└── playground.html         # Standalone API playground
```

---

## Production deploy (Railway)

The `railway.json` + `Procfile` are wired for Railway's Nixpacks
builder. On every deploy:

1. **Build** — `pip install -r requirements.txt && python scripts/build.py`
   (the build step minifies `frontend/static/*.css` + `*.js` to
   `*.min.css` / `*.min.js`)
2. **Start** — `alembic upgrade head && uvicorn src.main:app --workers 2 --port $PORT --timeout-keep-alive 300`
3. **Healthcheck** — Railway hits `/healthz`; deploy succeeds when it
   returns `{"status":"ok"}` within 30s.

### Required env vars in production

Copy from `.env.example`. The minimum to boot:

| Var | Why |
|-----|-----|
| `DATABASE_URL` | Postgres connection string (Railway Plugin provides this) |
| `JWT_SECRET_KEY` | 32+ char random; used to sign access + refresh tokens |
| `SESSION_SECRET_KEY` | OAuth state cookies (Authlib) + admin session |
| `OPENAI_API_KEY` | Agents, embeddings, vision |
| `RESEND_API_KEY` + `EMAIL_MODE=resend` | Verification / reset / receipts |
| `ADMIN_PASSWORD_HASH` | bcrypt of admin password |
| `ADMIN_EMAILS` | Comma-separated allowlist for admin login |

OAuth (Google / Facebook / Apple), AWS S3, VAPID, Sentry, Serper,
Jina — optional but degrade specific features when missing. See
[`.env.example`](.env.example) for the full list.

### Cutover checklist (Flask → FastAPI)

1. **Tag the v1 repo** — `git tag v1.0-flask-final && git push origin v1.0-flask-final` from the Flask repo
2. **Provision** the new FastAPI Railway service alongside the existing Flask one
3. **Pre-flight DB migration** — Alembic baseline `34b9481e6509` targets the post-WIP-deploy schema. Make sure the Flask WIP migrations have applied before pointing FastAPI at the same Postgres
4. **Smoke test on staging URL** — anonymous pages, register, login, fast-mode chat, full-mode chat, file upload, video stream, admin dashboard, push subscribe, OAuth (all three providers)
5. **DNS swap** — point the production domain at the FastAPI service
6. **Monitor Sentry** for 1 hour after cutover
7. **Rollback plan** — DNS swap back; the Flask service stays warm for at least the first day

---

## Migration invariants (preserved from v1)

These are the rules every code change in this repo must respect.
[CLAUDE.md](CLAUDE.md) explains each in detail with file pointers.

1. **Multi-tenancy** is closure-captured, not parameter-passed.
2. **Streaming chat is SSE**, byte-for-byte v1 format.
3. **Two chat modes** (fast / full) with a 3-layer router.
4. **Memory writes are background**, never block the response.
5. **Smart memory scoping** — style/preference/philosophy are cross-team;
   insight/decision/pattern/fact are team-specific.
6. **Auth dual-mode** — cookie AND Bearer.
7. **CSRF rules** — exempt list preserved exactly.
8. **Trial / purge** — club members exempt.
9. **OpenAI cost tracking** — every call logged.
10. **Research cache is tenant-keyed** (cross-coach leak fixed).
11. **No backwards-compat shims** — delete, don't `_legacy_`.
12. **English-first UI** — no Hebrew/RTL changes during migration.

---

## Reference

- **Agent guidance** (for Claude / Cursor / similar): [CLAUDE.md](CLAUDE.md)
- **Deferred items**: [MIGRATION_TODO.md](MIGRATION_TODO.md)
- **Schema baseline**: [schema_baseline.md](schema_baseline.md)
- **Live v1 source-of-truth**: sibling Flask repo, tag `v1.0-flask`
