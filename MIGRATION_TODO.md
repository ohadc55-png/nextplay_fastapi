# Migration TODOs

Tracked deferrals from the Flask → FastAPI migration. Each entry has phase, owner, and a decision date.

## Phase 0 — Foundation

- **Python runtime drift (local vs Railway).** Local default is 3.13.9 (anaconda); `runtime.txt` pins `python-3.11` for Railway parity with v1.0-flask. Most pinned deps support 3.13, but if anything fails locally, install Python 3.11 alongside or override `runtime.txt`. Decide before Phase 9 deploy.
- **Frontend not yet copied.** `frontend/` will be ported verbatim from `basketball_coach_ai/frontend/` in Phase 8. Until then, `/static` and HTML page routes are not registered — `/healthz` is the only endpoint.

## Phase 1 — Models

(empty)

## Phase 5 — AI

- **Research cache cross-coach bug.** Re-key cache at `backend/research/web_researcher.py:684-700` (in v1.0-flask) by `(user_id, team_id, query, level_hint, url_hint, hour_bucket)`. **Fix is part of Phase 5; do not skip.**

## Post-launch (out of migration scope)

- **Move file-processing pipelines to a queue.** PyMuPDF / openpyxl / pandas extraction is currently inline in chat-upload (UX expects sync). At scale, defer to a Celery/RQ worker.
- **Replace in-memory rate limiter with Redis.** The custom per-IP tracker (ported in Phase 7) is per-process; horizontal scaling needs Redis.
- **APScheduler / cron worker.** Push delivery currently relies on Railway hitting `/api/internal/run-push-jobs` externally. Consider an in-app scheduler post-launch.
