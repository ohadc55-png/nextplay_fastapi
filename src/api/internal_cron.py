"""Internal cron endpoints — Phase 2.6.

Two entry points an external scheduler calls (Railway cron / GitHub
Actions / Vercel cron):

  POST /api/internal/run-reminders                  — document reminders
  POST /api/internal/run-scheduled-messages          — promote SCHEDULED → SENDING

Both are secret-gated with `X-Cron-Secret` matching `settings.CRON_SECRET`.
Fail-closed: when CRON_SECRET is empty in env, the endpoints return 503 —
no one can run them, including the cron itself, until prod is configured.

Both accept `?dry_run=true` for a preview run that lists what would be sent
without sending anything. Safe to hit by hand to validate scope.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Query

from src.core.config import settings
from src.services import reminder_service, scheduled_message_worker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/internal", tags=["internal-cron"])


def _check_secret(x_cron_secret: str | None) -> None:
    expected = (settings.CRON_SECRET or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="CRON_SECRET not configured")
    if (x_cron_secret or "").strip() != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/run-reminders")
async def run_reminders(
    dry_run: bool = Query(default=False),
    limit: int = Query(default=reminder_service.MAX_PER_RUN, ge=1, le=2000),
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """Document reminders — see `reminder_service.run_reminders`.

    dry_run=true → returns a preview list, never sends.
    """
    _check_secret(x_cron_secret)
    return await reminder_service.run_reminders(dry_run=dry_run, limit=limit)


@router.post("/run-scheduled-messages")
async def run_scheduled_messages(
    dry_run: bool = Query(default=False),
    limit: int = Query(default=scheduled_message_worker.MAX_PER_RUN, ge=1, le=1000),
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """Scheduled messages — see `scheduled_message_worker.run_scheduled`."""
    _check_secret(x_cron_secret)
    return await scheduled_message_worker.run_scheduled(dry_run=dry_run, limit=limit)


@router.post("/run-calendar-pushes")
async def run_calendar_pushes(
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """Phase 15 — Coach Calendar push reminders.

    Runs `push_service.run_push_jobs()` which fires:
      - calendar_3h:      ~3h before each upcoming event
      - calendar_summary: ~1h after each event ends, only if no summary
                          was recorded
      - inactive_coach:   (pre-existing) inactive-user nudge

    Idempotent: each push checks `push_log` for a recent duplicate before
    sending. Safe to call every ~10 min.
    """
    from src.services import push_service

    _check_secret(x_cron_secret)
    return await push_service.run_push_jobs()


__all__ = ["router"]
