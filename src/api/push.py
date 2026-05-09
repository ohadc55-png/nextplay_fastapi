"""Web Push HTTP routes — async port of `backend/api/push_routes.py`.

Endpoints (auth required unless noted):
  GET  /api/push/vapid-key            — public; returns the VAPID public key
  POST /api/push/subscribe            — save a PushSubscription from the browser
  POST /api/push/unsubscribe          — remove a single endpoint + flip flag off
  GET  /api/push/preferences          — read push_enabled / quiet hours / tz
  POST /api/push/preferences          — update them (only keys present applied)
  POST /api/push/test                 — fire a test push to self (stub in Phase 4)

Internal:
  POST /api/internal/run-push-jobs    — cron entry point. Auth via X-Cron-Secret.
                                        Stub until Phase 7 delivers the runner.

Wire format matches v1 byte-for-byte so the existing frontend (`sw.js`,
`push.js`) keeps working without any client-side changes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.config import settings
from src.core.database import get_db
from src.models.users import User
from src.schemas.push import (
    PushPreferencesUpdate,
    PushSubscribeRequest,
    PushTestRequest,
    PushUnsubscribeRequest,
    VapidKeyResponse,
)
from src.services import push_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["push"])


# ---------------------------------------------------------------------------
# VAPID public key (no auth — safe to expose)
# ---------------------------------------------------------------------------

@router.get("/api/push/vapid-key", response_model=VapidKeyResponse)
async def push_vapid_key() -> VapidKeyResponse:
    """Return the VAPID public key. Only the matching private key (server-
    side) can sign push messages, so exposing this is safe."""
    key = settings.VAPID_PUBLIC_KEY
    return VapidKeyResponse(key=key or "", configured=bool(key))


# ---------------------------------------------------------------------------
# Subscribe / Unsubscribe
# ---------------------------------------------------------------------------

@router.post("/api/push/subscribe")
async def push_subscribe(
    body: PushSubscribeRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Persist a PushSubscription. Subscribing implies opt-in, so we flip
    `push_enabled = True` after the row lands. Optional `timezone` is
    captured for scheduling."""
    endpoint = body.endpoint.strip()
    p256dh = body.keys.p256dh.strip()
    auth = body.keys.auth.strip()
    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Missing endpoint or keys")

    user_agent = (request.headers.get("user-agent") or "")[:255]

    ok = await push_service.save_subscription(
        db,
        user_id=user.id,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        user_agent=user_agent,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save subscription")

    if body.timezone:
        await push_service.set_user_timezone(db, user.id, body.timezone.strip())

    await push_service.set_push_enabled(db, user.id, True)
    return {"ok": True, "push_enabled": True}


@router.post("/api/push/unsubscribe")
async def push_unsubscribe(
    body: PushUnsubscribeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove the named endpoint (if any) and flip the global toggle off.
    Empty body just flips the flag without deleting per-device rows — same
    semantics as v1."""
    endpoint = (body.endpoint or "").strip()
    if endpoint:
        await push_service.delete_subscription(db, endpoint)
    await push_service.set_push_enabled(db, user.id, False)
    return {"ok": True, "push_enabled": False}


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

@router.get("/api/push/preferences")
async def push_get_preferences(user: User = Depends(get_current_user)) -> dict:
    """Read the current preferences. Defaults match v1 fallbacks (quiet
    hours 22→7, timezone Asia/Jerusalem)."""
    return {
        "push_enabled": bool(user.push_enabled),
        "quiet_start": int(user.push_quiet_start) if user.push_quiet_start is not None else 22,
        "quiet_end": int(user.push_quiet_end) if user.push_quiet_end is not None else 7,
        "timezone": user.timezone or "Asia/Jerusalem",
    }


@router.post("/api/push/preferences")
async def push_set_preferences(
    body: PushPreferencesUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update only the keys actually present in the request. Mirrors v1's
    `if "x" in data` semantics — explicit `None` is treated as 'not present'.

    Quiet hours need both `quiet_start` AND `quiet_end` to apply (matching
    v1's coupled-pair behavior). Timezone validation rejects garbage names
    silently — the frontend just sees them missing from the `updated` echo."""
    updates: dict = {}

    if body.push_enabled is not None:
        if await push_service.set_push_enabled(db, user.id, body.push_enabled):
            updates["push_enabled"] = body.push_enabled

    if body.quiet_start is not None and body.quiet_end is not None:
        if await push_service.set_quiet_hours(
            db, user.id, body.quiet_start, body.quiet_end
        ):
            updates["quiet_start"] = body.quiet_start
            updates["quiet_end"] = body.quiet_end

    if body.timezone:
        tz = body.timezone.strip()
        if tz and await push_service.set_user_timezone(db, user.id, tz):
            updates["timezone"] = tz

    return {"ok": True, "updated": updates}


# ---------------------------------------------------------------------------
# Self-test push
# ---------------------------------------------------------------------------

@router.post("/api/push/test")
async def push_test(
    _body: PushTestRequest | None = None,
    user: User = Depends(get_current_user),
) -> dict:
    """Fire a test push to self. Bypasses daily cap + quiet hours so a
    coach can verify their device during onboarding. Real delivery lands
    in Phase 7; for now we ack so the UI flow works."""
    return await push_service.send_test_push(user_id=user.id)


# ---------------------------------------------------------------------------
# Cron entry point — internal, secret-gated
# ---------------------------------------------------------------------------

@router.post("/api/internal/run-push-jobs")
async def push_run_jobs(
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """External cron hits this hourly. The runner that decides who's due
    for which reminder (48h-inactive / roster-incomplete) lands in Phase 7.

    Auth is fail-closed: if `CRON_SECRET` isn't configured the endpoint
    refuses all calls (503), so an unconfigured prod can never silently
    expose this."""
    expected = (settings.CRON_SECRET or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="CRON_SECRET not configured")
    presented = (x_cron_secret or "").strip()
    if presented != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Phase 7 will wire `run_all_due_jobs()` here.
    logger.info("[push] cron tick — STUB (Phase 7 will wire scheduler)")
    return {"ok": True, "stats": {"stub": True}}
