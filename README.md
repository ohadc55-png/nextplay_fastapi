# NEXTPLAY (FastAPI)

> Migration target for [NEXTPLAY](https://nextplay.app) — the Flask v1.0 codebase is preserved at tag `v1.0-flask` in the legacy repo. This repo is being built **phase by phase** to preserve exact behavior. See the migration plan at `~/.claude/plans/nextplay-master-prompt-compressed-fox.md`.

## Phase 0 status: foundation

Empty FastAPI app with:
- Async SQLAlchemy engine (asyncpg in prod / aiosqlite in dev)
- Pydantic Settings config (every Flask env var ported with backward-compat aliases)
- `AppError` exception hierarchy + JSON handler
- Sentry integration (gated by `SENTRY_DSN`)
- CORS middleware
- Alembic baseline-ready scaffold (no migrations yet — Phase 1)
- `/healthz` endpoint

Routers, services, repositories, models, and AI integrations land in Phases 1–9.

## Quick start

```bash
# 1. Create venv + install deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Windows PowerShell
pip install -r requirements.txt

# 2. Configure
copy .env.example .env
# Fill in OPENAI_API_KEY + JWT_SECRET_KEY at minimum.

# 3. Run
uvicorn src.main:app --reload --port 5050

# 4. Verify
curl http://localhost:5050/healthz
# -> {"status":"ok","db":"connected","version":"2.0.0-alpha","environment":"local"}
```

## Project layout

```
nextplay_fastapi/
├── src/
│   ├── api/            # Routers (Phase 3-9)
│   │   └── deps/       # FastAPI dependencies (auth, db, csrf)
│   ├── core/           # Config, async engine, JSONText, exceptions, Base, TimestampMixin
│   ├── middleware/     # CSRF, security headers, rate limiter (Phase 3, 7)
│   ├── models/         # SQLAlchemy 2.0 ORM (Phase 1)
│   ├── schemas/        # Pydantic API request/response shapes (Phase 2)
│   ├── repositories/   # Async data access; tenant-scoped base (Phase 2)
│   ├── services/       # Business logic (Phase 3-7)
│   ├── auth/           # JWT, OAuth, purge state machine (Phase 3)
│   ├── crew/           # CrewAI orchestration, routing, llm wrapper (Phase 5)
│   ├── research/       # 8-stage web research pipeline (Phase 5)
│   └── main.py         # FastAPI app
│
├── alembic/            # Schema migrations (Phase 1+)
├── tests/              # pytest-asyncio (Phase 9)
├── requirements.txt
├── Procfile            # Railway: uvicorn + workers
├── runtime.txt         # python-3.11
└── railway.json        # Nixpacks build
```

## Migration discipline

**The One Rule:** every line of FastAPI code must do exactly the same thing as the Flask version it replaces — same edge cases, same security model, same multi-tenancy, same response shape, same error text, same status codes. If you can't answer "yes" to that — STOP and ask.

See the master prompt in conversation context for the full list of architecture invariants and known bugs targeted for fix during migration.
