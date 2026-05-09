"""Push notification service — preferences + subscription persistence.

Async port of `backend/push/preferences.py` + `backend/push/subscriptions.py`.
Delivery (pywebpush) and scheduling are out of scope here — they land in
Phase 7 alongside the cron runner. This module only covers what the
`/api/push/*` routes need: read/write user prefs and upsert the
`push_subscriptions` row from a browser-supplied PushSubscription.

Validation rules mirror v1 exactly:
  - quiet hours must both fall in 0-23 (silently ignored otherwise)
  - timezone must parse via `zoneinfo.ZoneInfo` AND be ≤64 chars
  - subscribing implies opt-in (`push_enabled = True`)
  - unsubscribing flips `push_enabled = False`
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import update

from src.models.push import PushSubscription
from src.models.users import User
from src.repositories.push_repo import PushSubscriptionsRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

async def set_push_enabled(session: "AsyncSession", user_id: int, enabled: bool) -> bool:
    """Toggle the user's `push_enabled` flag. Returns True on success."""
    try:
        await session.execute(
            update(User).where(User.id == user_id).values(push_enabled=bool(enabled))
        )
        await session.flush()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[push] set_push_enabled(uid=%s, %s) failed: %s", user_id, enabled, e)
        return False


async def set_quiet_hours(
    session: "AsyncSession", user_id: int, start: int, end: int
) -> bool:
    """Set quiet hours window (start/end as integer hour 0-23). Mirrors v1
    silent-ignore-on-bad-input behavior."""
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return False
    try:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(push_quiet_start=int(start), push_quiet_end=int(end))
        )
        await session.flush()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[push] set_quiet_hours(uid=%s) failed: %s", user_id, e)
        return False


def _is_valid_timezone(tz_name: str) -> bool:
    if not tz_name or len(tz_name) > 64:
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz_name)
        return True
    except Exception:  # noqa: BLE001
        return False


async def set_user_timezone(
    session: "AsyncSession", user_id: int, tz_name: str
) -> bool:
    """Persist an IANA timezone name. Validated via ZoneInfo before write so
    bad clients can't poison the column."""
    if not _is_valid_timezone(tz_name):
        logger.debug("[push] set_user_timezone rejected invalid tz: %r", tz_name)
        return False
    try:
        await session.execute(
            update(User).where(User.id == user_id).values(timezone=tz_name)
        )
        await session.flush()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[push] set_user_timezone(uid=%s) failed: %s", user_id, e)
        return False


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

async def save_subscription(
    session: "AsyncSession",
    *,
    user_id: int,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None,
) -> bool:
    """Upsert a PushSubscription on (endpoint). Resubscribing the same browser
    should rebind the row to the current user (mirrors v1 ON CONFLICT semantics)."""
    repo = PushSubscriptionsRepository(session)
    try:
        existing = await repo.get_by_endpoint(endpoint)
        if existing:
            existing.user_id = user_id
            existing.p256dh = p256dh
            existing.auth = auth
            existing.user_agent = (user_agent or "")[:255] or None
            await session.flush()
            return True

        sub = PushSubscription(
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=(user_agent or "")[:255] or None,
        )
        await repo.create(sub)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[push] save_subscription(uid=%s) failed: %s", user_id, e)
        return False


async def delete_subscription(session: "AsyncSession", endpoint: str) -> int:
    """Hard-delete a single subscription row (the device just unsubscribed)."""
    return await PushSubscriptionsRepository(session).delete_by_endpoint(endpoint)


# ---------------------------------------------------------------------------
# Test push (stub — full delivery lands in Phase 7)
# ---------------------------------------------------------------------------

async def send_test_push(*, user_id: int) -> dict:
    """Phase 7 will wire pywebpush here. For now we acknowledge so the UI's
    'Send test' button can confirm the auth + preference plumbing works.
    The frontend treats a non-error response as success."""
    logger.info("[push] send_test_push uid=%s — STUB (Phase 7 delivery pending)", user_id)
    return {"ok": True, "stub": True, "detail": "test push queued (delivery wiring in Phase 7)"}


__all__ = [
    "set_push_enabled",
    "set_quiet_hours",
    "set_user_timezone",
    "save_subscription",
    "delete_subscription",
    "send_test_push",
]
