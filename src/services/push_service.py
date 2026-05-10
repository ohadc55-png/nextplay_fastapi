"""Push notification service — preferences, subscriptions, AND delivery.

Async port of `backend/push/{preferences,subscriptions,sender}.py`.
Phase 7 batch 2 wires the delivery (pywebpush via asyncio.to_thread)
and the gate stack:

  Gate 1 — push_enabled flag (opt-in)
  Gate 2 — at least one active subscription
  Gate 3 — quiet hours (skipped when force=True)
  Gate 4 — daily cap of 1 push per 22h (skipped when force=True)

Every decision (sent / skipped / failed) writes a `push_log` row so
the admin panel can show why a coach didn't get a push, AND so the
cap is enforceable across process restarts.

Mode selection:
  - VAPID_PRIVATE_KEY missing → console mode (renders to stdout, logs as 'sent')
  - VAPID_PRIVATE_KEY present → real push via pywebpush

Validation rules (preferences) mirror v1 exactly:
  - quiet hours must both fall in 0-23 (silently ignored otherwise)
  - timezone must parse via `zoneinfo.ZoneInfo` AND be ≤64 chars
  - subscribing implies opt-in (`push_enabled = True`)
  - unsubscribing flips `push_enabled = False`
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func as sql_func
from sqlalchemy import select, update

from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.models.push import PushLog, PushSubscription
from src.models.users import User
from src.repositories.push_repo import PushLogRepository, PushSubscriptionsRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# Skip reasons — re-export for the admin UI / tests
REASON_NOT_ENABLED = "not_enabled"
REASON_NO_SUBSCRIPTION = "no_subscription"
REASON_QUIET_HOURS = "quiet_hours"
REASON_DAILY_CAP = "daily_cap"
REASON_HTTP_ERROR = "http_error"

DAILY_CAP_HOURS = 22.0


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

async def set_push_enabled(session: AsyncSession, user_id: int, enabled: bool) -> bool:
    """Toggle the user's `push_enabled` flag. Returns True on success."""
    try:
        await session.execute(
            update(User).where(User.id == user_id).values(push_enabled=bool(enabled))
        )
        await session.flush()
        return True
    except Exception as e:
        logger.warning("[push] set_push_enabled(uid=%s, %s) failed: %s", user_id, enabled, e)
        return False


async def set_quiet_hours(
    session: AsyncSession, user_id: int, start: int, end: int
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
    except Exception as e:
        logger.warning("[push] set_quiet_hours(uid=%s) failed: %s", user_id, e)
        return False


def _is_valid_timezone(tz_name: str) -> bool:
    if not tz_name or len(tz_name) > 64:
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz_name)
        return True
    except Exception:
        return False


async def set_user_timezone(
    session: AsyncSession, user_id: int, tz_name: str
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
    except Exception as e:
        logger.warning("[push] set_user_timezone(uid=%s) failed: %s", user_id, e)
        return False


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

async def save_subscription(
    session: AsyncSession,
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
    except Exception as e:
        logger.warning("[push] save_subscription(uid=%s) failed: %s", user_id, e)
        return False


async def delete_subscription(session: AsyncSession, endpoint: str) -> int:
    """Hard-delete a single subscription row (the device just unsubscribed)."""
    return await PushSubscriptionsRepository(session).delete_by_endpoint(endpoint)


# ---------------------------------------------------------------------------
# Delivery — gate stack + pywebpush
# ---------------------------------------------------------------------------


def _push_mode() -> str:
    """'send' if VAPID keys are configured, else 'console'."""
    priv = (settings.VAPID_PRIVATE_KEY or "").strip()
    pub = (settings.VAPID_PUBLIC_KEY or "").strip()
    return "send" if (priv and pub) else "console"


def _safe_print(s: str) -> None:
    try:
        print(s)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(s.encode(enc, errors="replace").decode(enc, errors="replace"))


def _render_to_console(
    *, user_id: int | None, title: str, body: str,
    deep_link: str | None, kind: str, sub_count: int,
) -> None:
    sep = "-" * 70
    _safe_print(f"\n{sep}\n[PUSH/console] kind={kind} uid={user_id} subs={sub_count}")
    _safe_print(f"Title:    {title}")
    _safe_print(f"Body:     {body}")
    if deep_link:
        _safe_print(f"Tap URL:  {deep_link}")
    _safe_print(sep + "\n")


def _send_via_pywebpush_sync(
    *, subscription: dict[str, Any], payload: dict[str, Any],
) -> tuple[bool, int | None, str | None]:
    """One pywebpush call. Returns (success, http_status, error_message).
    A 404/410 means the subscription is dead → caller should delete it."""
    try:
        from pywebpush import WebPushException, webpush
    except Exception as e:
        return False, None, f"pywebpush not installed: {e}"

    priv = (settings.VAPID_PRIVATE_KEY or "").strip()
    if not priv:
        return False, None, "VAPID_PRIVATE_KEY missing"

    sub_info = {
        "endpoint": subscription["endpoint"],
        "keys": {
            "p256dh": subscription["p256dh"],
            "auth": subscription["auth"],
        },
    }
    try:
        webpush(
            subscription_info=sub_info,
            data=json.dumps(payload),
            vapid_private_key=priv,
            vapid_claims={"sub": settings.VAPID_SUBJECT or "mailto:noreply@nextplay.example"},
            ttl=86400,  # push service holds the message up to 24h if device is offline
        )
        return True, 201, None
    except WebPushException as e:
        status = getattr(e.response, "status_code", None) if getattr(e, "response", None) else None
        return False, status, f"WebPushException: {e}"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


async def _log_push(
    session: AsyncSession,
    *,
    user_id: int | None,
    title: str,
    body: str,
    deep_link: str | None,
    kind: str,
    status: str,
    reason: str | None = None,
    error: str | None = None,
) -> None:
    """Best-effort push_log insert."""
    try:
        session.add(PushLog(
            user_id=user_id,
            title=(title or "")[:140],
            body=(body or "")[:280],
            deep_link=(deep_link or "")[:255] or None,
            kind=(kind or "")[:32],
            status=status,
            reason=(reason or None) and reason[:60],
            error=(error or None) and error[:500],
        ))
        await session.flush()
    except Exception as e:
        logger.debug("[push] _log_push failed: %s", e)


async def _is_in_quiet_hours(user: User) -> bool:
    """Check if NOW (in user's timezone) falls inside their quiet window.

    Defaults: 22→7 (10pm to 7am). The window can wrap midnight; both
    directions are handled. Mirrors v1 push.preferences.is_in_quiet_hours."""
    start = user.push_quiet_start
    end = user.push_quiet_end
    if start is None or end is None:
        return False
    tz_name = user.timezone or "Asia/Jerusalem"
    try:
        from zoneinfo import ZoneInfo

        now_h = datetime.now(ZoneInfo(tz_name)).hour
    except Exception:
        # Bad tz string → fail open (no quiet hours rather than silent block)
        return False
    if start == end:
        return False
    if start < end:
        # Window doesn't cross midnight (e.g. 13→17)
        return start <= now_h < end
    # Wraps midnight (e.g. 22→7)
    return now_h >= start or now_h < end


async def send_push_to_user(
    session: AsyncSession,
    *,
    user_id: int,
    title: str,
    body: str,
    deep_link: str | None = None,
    kind: str = "engagement",
    force: bool = False,
) -> dict:
    """Send a push to one user. Honors all gates.

    Returns: {status: 'sent'|'skipped'|'failed', reason, sent_count}

    `force=True` skips daily cap + quiet hours — used for the 'Send
    test push' button so coaches can verify delivery during onboarding.
    Never raises; all failures land in push_log."""
    user = (await session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if user is None:
        await _log_push(
            session, user_id=user_id, title=title, body=body,
            deep_link=deep_link, kind=kind,
            status="failed", error="user_not_found",
        )
        return {"status": "failed", "reason": "user_not_found", "sent_count": 0}

    # Gate 1 — push_enabled
    if not force and not user.push_enabled:
        await _log_push(
            session, user_id=user_id, title=title, body=body,
            deep_link=deep_link, kind=kind,
            status="skipped", reason=REASON_NOT_ENABLED,
        )
        return {"status": "skipped", "reason": REASON_NOT_ENABLED, "sent_count": 0}

    # Gate 2 — any subscription?
    subs_repo = PushSubscriptionsRepository(session)
    subs = await subs_repo.list_for_user(user_id)
    if not subs:
        await _log_push(
            session, user_id=user_id, title=title, body=body,
            deep_link=deep_link, kind=kind,
            status="skipped", reason=REASON_NO_SUBSCRIPTION,
        )
        return {"status": "skipped", "reason": REASON_NO_SUBSCRIPTION, "sent_count": 0}

    # Gate 3 — quiet hours
    if not force and await _is_in_quiet_hours(user):
        await _log_push(
            session, user_id=user_id, title=title, body=body,
            deep_link=deep_link, kind=kind,
            status="skipped", reason=REASON_QUIET_HOURS,
        )
        return {"status": "skipped", "reason": REASON_QUIET_HOURS, "sent_count": 0}

    # Gate 4 — daily cap
    if not force:
        log_repo = PushLogRepository(session)
        if await log_repo.has_recent_send(user_id=user_id, hours=int(DAILY_CAP_HOURS)):
            await _log_push(
                session, user_id=user_id, title=title, body=body,
                deep_link=deep_link, kind=kind,
                status="skipped", reason=REASON_DAILY_CAP,
            )
            return {"status": "skipped", "reason": REASON_DAILY_CAP, "sent_count": 0}

    payload = {
        "title": title,
        "body": body,
        "url": deep_link or "/",
        "kind": kind,
        "icon": "/static/img/icons/icon-192.png",
        "tag": kind,  # same kind replaces the previous notification of that kind
    }
    mode = _push_mode()

    if mode == "console":
        _render_to_console(
            user_id=user_id, title=title, body=body,
            deep_link=deep_link, kind=kind, sub_count=len(subs),
        )
        await _log_push(
            session, user_id=user_id, title=title, body=body,
            deep_link=deep_link, kind=kind, status="sent",
        )
        await session.execute(
            update(User).where(User.id == user_id).values(last_push_sent_at=sql_func.now()),
        )
        await session.flush()
        return {"status": "sent", "reason": "console_mode", "sent_count": len(subs)}

    # Real pywebpush. Each call goes off-thread because pywebpush is sync.
    sent_count = 0
    last_error: str | None = None
    dead_endpoints: list[str] = []
    for sub in subs:
        sub_info = {
            "endpoint": sub.endpoint,
            "p256dh": sub.p256dh,
            "auth": sub.auth,
        }
        ok, status_code, error = await asyncio.to_thread(
            _send_via_pywebpush_sync, subscription=sub_info, payload=payload,
        )
        if ok:
            sent_count += 1
            await subs_repo.touch_last_used(sub.id)
        else:
            last_error = error
            # 404 / 410 → subscription is dead, sweep it
            if status_code in (404, 410):
                dead_endpoints.append(sub.endpoint)
    for endpoint in dead_endpoints:
        await subs_repo.delete_by_endpoint(endpoint)

    if sent_count > 0:
        await _log_push(
            session, user_id=user_id, title=title, body=body,
            deep_link=deep_link, kind=kind, status="sent",
        )
        await session.execute(
            update(User).where(User.id == user_id).values(last_push_sent_at=sql_func.now()),
        )
        await session.flush()
        return {"status": "sent", "reason": None, "sent_count": sent_count}

    await _log_push(
        session, user_id=user_id, title=title, body=body,
        deep_link=deep_link, kind=kind,
        status="failed", reason=REASON_HTTP_ERROR, error=last_error,
    )
    return {"status": "failed", "reason": REASON_HTTP_ERROR, "sent_count": 0}


async def send_push_task(
    *,
    user_id: int,
    title: str,
    body: str,
    deep_link: str | None = None,
    kind: str = "engagement",
    force: bool = False,
) -> None:
    """Background-task entrypoint. Opens its own session because by the
    time this runs, the request's `get_db` has already committed and
    closed. NEVER raises."""
    try:
        async with AsyncSessionLocal() as session:
            try:
                await send_push_to_user(
                    session,
                    user_id=user_id, title=title, body=body,
                    deep_link=deep_link, kind=kind, force=force,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("[push] background task failed for uid=%s", user_id)
    except Exception:
        logger.exception("[push] could not open session for delivery")


async def send_test_push(session: AsyncSession, *, user_id: int) -> dict:
    """Synchronous test push — bypasses cap + quiet hours. Used by the
    `/api/push/test` endpoint so the coach gets immediate feedback.

    Runs through the request's session so the user can see the result
    of their just-saved subscription without a session-bridge hop."""
    try:
        return await send_push_to_user(
            session,
            user_id=user_id,
            title="NEXTPLAY test",
            body="Push notifications are working!",
            deep_link="/settings",
            kind="test",
            force=True,
        )
    except Exception:
        logger.exception("[push] test push failed for uid=%s", user_id)
        return {"status": "failed", "reason": "internal_error", "sent_count": 0}


# ---------------------------------------------------------------------------
# Cron runner — Phase 7 stub-runner that picks who's due and pushes
# ---------------------------------------------------------------------------


async def run_push_jobs() -> dict:
    """Hourly cron entrypoint. Today's scope: a single 'inactive coach'
    job — coaches whose last_seen_at is between INACTIVE_HOURS and 7 days
    ago AND who have no recent push get a friendly nudge.

    More job kinds (roster-incomplete, trial-ending, weekly-recap) port
    in follow-ups. The runner's shape is set so adding a job is one
    function call; the gate stack in `send_push_to_user` does the rest.
    """
    stats = {"jobs_run": 0, "sent": 0, "skipped": 0, "failed": 0}
    try:
        async with AsyncSessionLocal() as session:
            try:
                stats = await _run_inactive_coach_job(session, stats)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("[push] cron job failed mid-run")
    except Exception:
        logger.exception("[push] cron could not open session")
    return stats


async def _run_inactive_coach_job(
    session: AsyncSession, stats: dict[str, int],
) -> dict[str, int]:
    """Find coaches inactive for more than `PUSH_INACTIVE_HOURS` hours and
    push them a 'come back to your team' note. Mirrors v1 push.scheduling
    behavior at a smaller scope (one job kind for now)."""
    inactive_hours = int(getattr(settings, "PUSH_INACTIVE_HOURS", 48) or 48)
    cutoff_recent = datetime.now(UTC) - timedelta(hours=inactive_hours)
    cutoff_floor = datetime.now(UTC) - timedelta(days=7)

    # Pick users with last_seen_at in [floor, recent_cutoff] AND push_enabled
    rows = list((await session.execute(
        select(User).where(
            User.push_enabled.is_(True),
            User.last_seen_at.is_not(None),
            User.last_seen_at >= cutoff_floor,
            User.last_seen_at < cutoff_recent,
        )
    )).scalars().all())

    if not rows:
        return stats

    stats["jobs_run"] += 1
    for u in rows:
        try:
            r = await send_push_to_user(
                session,
                user_id=u.id,
                title="Your team is waiting",
                body="Quick check-in on practice plans + scouting?",
                deep_link="/chat",
                kind="inactive_coach",
            )
            stats[r["status"]] = stats.get(r["status"], 0) + 1
        except Exception as e:
            logger.warning("[push] cron uid=%s send failed: %s", u.id, e)
            stats["failed"] = stats.get("failed", 0) + 1
    return stats


__all__ = [
    "DAILY_CAP_HOURS",
    "REASON_DAILY_CAP",
    "REASON_HTTP_ERROR",
    "REASON_NOT_ENABLED",
    "REASON_NO_SUBSCRIPTION",
    "REASON_QUIET_HOURS",
    "delete_subscription",
    "run_push_jobs",
    "save_subscription",
    "send_push_task",
    "send_push_to_user",
    "send_test_push",
    "set_push_enabled",
    "set_quiet_hours",
    "set_user_timezone",
]
