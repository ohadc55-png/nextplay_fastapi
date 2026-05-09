# Migration TODOs

Tracked deferrals from the Flask → FastAPI migration. Each entry has phase, owner, and a decision date.

## Phase 0 — Foundation

- **Python runtime drift (local vs Railway).** Local default is 3.13.9 (anaconda); `runtime.txt` pins `python-3.11` for Railway parity with v1.0-flask. Most pinned deps support 3.13, but if anything fails locally, install Python 3.11 alongside or override `runtime.txt`. Decide before Phase 9 deploy.
- **Frontend not yet copied.** `frontend/` will be ported verbatim from `basketball_coach_ai/frontend/` in Phase 8. Until then, `/static` and HTML page routes are not registered — `/healthz` is the only endpoint.

## Phase 2 — Repositories

- **`PlayerMetricsRepository.upsert` UPDATE-path test deferred.** The
  aiosqlite + autoflush=False + UPDATE-on-JSONText-column combination
  surfaces a SQLAlchemy `MissingGreenlet` in `dialects/sqlite/aiosqlite.py`
  that I couldn't isolate in a few hours of debugging. The repo logic
  itself is straightforward (a guarded `update().values(...)`). The INSERT
  branch is tested. We'll write the UPDATE-branch test in Phase 4 when the
  upsert is wired into an actual `/api/players/<id>/metrics` endpoint and
  exercised end-to-end against asyncpg, where the greenlet bridge works
  differently.

## Phase 1 — Models

### Decisions logged (parity-first)

- **`memories.embedding_json` stays as `JSONText`** (1536-dim float vector serialized as JSON string), matching v1.0-flask. **Future:** when we want vector-similarity search at scale, migrate the column type to `pgvector` and add an `ivfflat`/`hnsw` index. Cosine similarity today is computed in Python after a SQL fetch — fine for the user volumes the app currently serves.
- **`storage_quota` singleton (`id = 1`)** preserved as-is. Anti-pattern but matches v1.0-flask semantics. Reconsider post-migration if/when multi-tenant quota is needed.
- **`team_profile.id` uses Postgres `SERIAL`** uniformly (no `DEFAULT 1` SQLite shim from v1.0-flask). The legacy default was an artifact of single-team mode and is no longer reachable.
- **Inline `CREATE TABLE` in v1.0-flask routes** (`ip_geo_cache` in `backend/admin/routes.py`, `sales_inquiries` in `backend/api/admin.py`) become first-class models in `src/models/`. The inline definitions in v1.0-flask stay until those routes are ported in Phase 4.
- **`mailing_lists` + `mailing_list_members`** included in models (added by `add_email_infrastructure.py` migration; not in earlier inventory drafts).

## Phase 5 — AI

- **Research cache cross-coach bug.** Re-key cache at `backend/research/web_researcher.py:684-700` (in v1.0-flask) by `(user_id, team_id, query, level_hint, url_hint, hour_bucket)`. **Fix is part of Phase 5; do not skip.**

## Pre-cutover gate — DEPLOY FLASK WIP TO PROD FIRST

The FastAPI baseline (rev `34b9481e6509`) targets the **post-WIP-deploy
Flask schema** (47 tables). Production Postgres is currently at the
**pre-WIP state** (38 tables). Before flipping Railway to FastAPI, the WIP
commit (`45f9ebd` in v1.0-flask repo) must be deployed to prod so the
idempotent v1.0-flask migrations create the missing schema.

**Specifically, deploying Flask WIP adds:**
- 9 tables: `auth_tokens`, `email_log`, `mailing_lists`,
  `mailing_list_members`, `push_subscriptions`, `push_log`,
  `notebook_entry_players`, `research_url_log`, `sales_inquiries`
- 10 columns on `users`: `email_marketing`, `unsubscribe_token`,
  `email_infra_signup`, `push_enabled`, `push_quiet_start`,
  `push_quiet_end`, `last_push_sent_at`, `last_seen_at`, `timezone`,
  `data_purge_at`
- 1 column on `memories`: `embedding_json` (1536-dim vector)

Migrations responsible (all idempotent, all in v1.0-flask `backend/migrations/`):
- `add_email_infrastructure.py`
- `add_push_infrastructure.py`
- `add_notebook_entry_players.py`
- `add_research_url_log.py`
- `add_data_purge_at.py`
- `add_memory_embeddings.py`
- (`sales_inquiries` is created inline by `backend/api/admin.py` on first
  endpoint hit — verified prod doesn't have it yet)

**Verification before cutover:** re-run `tools/diff_prod_schema.py`. Expect
zero drift on shared tables AND zero "tables in models, missing from
prod". If either is non-zero, the WIP deploy did not complete; investigate
before flipping the Procfile.

---

## Post-launch (out of migration scope)

- **Move file-processing pipelines to a queue.** PyMuPDF / openpyxl / pandas extraction is currently inline in chat-upload (UX expects sync). At scale, defer to a Celery/RQ worker.
- **Replace in-memory rate limiter with Redis.** The custom per-IP tracker (ported in Phase 7) is per-process; horizontal scaling needs Redis.
- **APScheduler / cron worker.** Push delivery currently relies on Railway hitting `/api/internal/run-push-jobs` externally. Consider an in-app scheduler post-launch.
