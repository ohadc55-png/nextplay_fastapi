# Migration TODOs

Tracked deferrals from the Flask → FastAPI migration. Each entry has phase, owner, and a decision date.

## Phase 0 — Foundation

- **Python runtime drift (local vs Railway).** Local default is 3.13.9 (anaconda); `runtime.txt` pins `python-3.11` for Railway parity with v1.0-flask. Most pinned deps support 3.13, but if anything fails locally, install Python 3.11 alongside or override `runtime.txt`. Decide before Phase 9 deploy.
- **Frontend not yet copied.** `frontend/` will be ported verbatim from `basketball_coach_ai/frontend/` in Phase 8. Until then, `/static` and HTML page routes are not registered — `/healthz` is the only endpoint.

## Phase 0 Enterprise — Multi-org foundation

- **RLS verified against Postgres (deferred to staging).** The Postgres RLS migration `9a3f2b1c4e7d_add_org_rls_postgres` is a no-op on SQLite (test runtime). Before flipping Railway to FastAPI Enterprise, run `python tools/verify_rls.py` against staging Postgres and require exit-0. The CI alembic-smoke step uses SQLite and therefore does NOT cover RLS.
- **Background tasks bypass `get_db` and the org GUC.** Memory extraction (`src/services/chat_service.py`) and email sender (`src/services/email.py`) open their own `AsyncSessionLocal()`, so `app.current_org_id` is unset during their work. Phase 0 background tasks don't touch org-scoped tables — safe. Any Phase 1+ background work that touches `team_profile`, `regions`, `branches`, `user_organizations`, `org_invites`, or `org_audit_logs` must explicitly `SELECT set_config('app.current_org_id', ...)` at the top of its session.
- **Clubs → Orgs migration.** The legacy `clubs` table (B2B v1) and the new `organizations` table coexist for now. A future phase will migrate club members to a synthesized organization, retire `users.club_id` / `users.is_club_admin`, and drop the `clubs` / `invite_codes` tables. Out of scope for Phase 0.
- **Org Admin HTML pages beyond Phase 0 set.** Only login, role-select, dashboard, and invite-accept landing exist. Members / branches / regions / teams / audit pages land in Phase 1 alongside the bulk-import flow for the 640-team Sha'ar Shivyon dataset.
- **System Admin HTML pages for orgs.** `/admin/api/orgs/*` JSON endpoints ship in Phase 0; the corresponding `/admin/orgs` and `/admin/orgs/{id}` HTML pages are deferred to Phase 1 (matches the existing pattern where admin JSON shipped before HTML).

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

- **Research cache cross-coach bug.** Re-key cache at `backend/research/web_researcher.py:684-700` (in v1.0-flask) by `(user_id, team_id, query, level_hint, url_hint, hour_bucket)`. **Fix is part of Phase 5; do not skip.** ✅ DONE in batch 8 — see [src/research/cache.py](src/research/cache.py).
- **Research pipeline Stages 5-7 (Extract / Verify / Synthesize).** Batches 8 + 8b ship Stages 0-4 (URL hint, Plan, Search, Triage, multi-Fetch). The fetched content is returned as `summary` so the calling agent's persona can extract directly. The structured per-page extract + cross-source verify + scout-report synthesize pipeline (gpt-4o, ~530 lines of prompts in v1 `prompts.py:155-528`) needs its own batch with careful per-stage testing. ✅ Stages 1-4 DONE in batch 8b.
- **CrewAI multi-agent orchestration.** ✅ DONE in batch 10 — see [src/crew/manager.py](src/crew/manager.py). Multi-agent delegation (e.g., Brad → Hunter scout job) deferred — today each turn runs ONE specialist; routing layer (batch 4) picks which one.
- **Vision pipeline** (GPT-4o Vision describe → specialist agent process). ✅ DONE in batch 9 — see [src/services/vision.py](src/services/vision.py). Wired-in via `/api/chat-upload` endpoint lands in Phase 7 (file processor batch).
- **KB document ingestion script.** Batch 6 ships the wrapper but no documents. Port the `knowledge_base/documents/` chunking + embedding script before launch.

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

---

## Phase 2 — Deferred work

Captured at the end of Phase 2 closeout (2026-05-12). All items have
"stable ground" — code is ready for the addition without restructuring.

### Tracked but deferred (in priority order)

- **Real SMS provider integration (2.7b).** `SMS_PROVIDER=twilio|inforu|o19|meta_whatsapp` currently raise `NotImplementedError` with a helpful pointer to the file path to create. Drop a new adapter inheriting `RealSMSProvider` from [src/services/sms/base.py](src/services/sms/base.py) and replace the corresponding branch in [src/services/sms/factory.py](src/services/sms/factory.py). Safety rails (kill switch + whitelist + audit) apply automatically. **Blocker:** vendor decision pending (Twilio vs Inforu/019 vs Meta Cloud API).
- **Railway healthcheck failure since `eea00a0`.** Production deploy is blocked. Last known good build, then the start.sh `set -e` + `alembic upgrade head` step appears to fail. Check Railway build logs before next deploy attempt. Workaround: roll back to last green if urgency demands.
- **Rotate `RESEND_API_KEY`.** The key was visible in a `grep` output during development; treat as exposed. Generate a fresh key in Resend dashboard, replace in `.env` (local) and Railway (prod).
- **Two-step campaign send (DRAFT → Send).** Current flow creates + dispatches in one POST. Spec asked for separate steps for approval workflows. Refactor: split `org_document_campaigns.create_and_send` into `POST /` (creates DRAFT) + `POST /{id}/send` (promotes to SENDING). The `DocumentCampaign.status` column already supports `DRAFT`.

### Polish / small wins

- **Hebrew-shaping PDF footer.** [src/services/pdf_generation_service.py](src/services/pdf_generation_service.py) writes the audit footer in ASCII only. Bundle [Heebo](https://fonts.google.com/specimen/Heebo) into the repo and switch `fitz` to use it for the footer.
- **DOCX preview rendering.** Today shows a placeholder PNG; needs `libreoffice --headless --convert-to pdf` round-trip. Affects only [src/services/document_template_service.py:render_template_preview](src/services/document_template_service.py).
- **True PDF attachment vs presigned link.** [src/services/document_signed_email.py](src/services/document_signed_email.py) sends a 7-day presigned S3 link in the body. Resend's attachments API supports inline PDFs — switch when needed.
- **Excel export.** [frontend/static/js/org-template-deliveries.js](frontend/static/js/org-template-deliveries.js) emits CSV (UTF-8 BOM; Excel opens directly). True .xlsx export would need `openpyxl` server-side or a JS lib — skipped because CSV is sufficient.
- **Per-row PII audit.** Current audit is aggregate-per-campaign (`player.contact.read` with `extra={"player_count": N}`). Regulators may eventually want per-row. The DocumentDelivery table already gives row-level traceability, so this is regulator-driven, not technical.
- **3-step wizard for campaign creation.** Current UI is a single modal with cascading region → branch → team dropdowns. If product asks for an explicit wizard (template → recipients → review), the existing endpoint shape supports it.

### Phase 3 candidates (named by Phase 2 spec)

- **Payments** — Stripe / Tranzila + payment plans + recurring + invoices. Treasurer role activates here.
- **Calendar** — training / game scheduling.
- **Facilities** — venue management.
