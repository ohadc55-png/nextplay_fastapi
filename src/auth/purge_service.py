"""Trial / data-purge state machine — async port of `backend/auth/purge.py`.

Per user spec (2026-05-04):
  - Account row stays (so user can log back in / upgrade)
  - All team-owned content is permanently deleted
  - Anonymous analytics rows (api_usage_logs, page_views, ip_geo_cache,
    research_url_log) are KEPT for product analytics
  - Auth artifacts (refresh_tokens, social_accounts) are KEPT so the user
    can still log in to upgrade
  - **Club members (club_id IS NOT NULL) are exempt from purge** — Club
    pays for them.

Two state transitions live here:

1. `flip_to_expired_and_schedule_purge(user_id)` — when a trial-active user
   is detected past `trial_ends_at`, mark them `expired` and schedule
   `data_purge_at = NOW + 30 days`. Idempotent (uses COALESCE so we don't
   re-shift the purge date on subsequent calls).

2. `purge_user_data(user_id)` — delete all team-owned content for `user_id`,
   then clear `data_purge_at` so we don't re-run on next request. Best-
   effort: a failure on a single table logs and continues.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.users import User

logger = logging.getLogger(__name__)


# Tables that have a `user_id` column. Order is FK-safe: child rows are
# deleted before their parents. Each entry is the table name as it appears
# in the schema.
_DIRECT_DELETE_TABLES: list[str] = [
    # memory / chat first (most ephemeral)
    "entity_observations",
    "entities",
    "session_summaries",
    "memories",
    "conversations",
    # notebook
    "notebook_entries",  # cascades attendance via FK ON DELETE CASCADE
    # scouting / video hub
    "scouting_players",
    "clip_playlists",
    "scouting_videos",  # cascades clips/annotations via FK ON DELETE CASCADE
    # plays / drills
    "plays",
    "play_shares",
    "compile_cards",
    # team & player content
    "player_game_stats",
    "player_metrics",
    "players",
    "team_profile",
    # uploads (DB row only — disk files cleaned separately)
    "uploads",
    # coach personalization
    "coach_preferences",
    "onboarding_events",
    "feedback",
]


# Tables WITHOUT a `user_id` column. Each one is deleted via a subquery
# against a parent table that DOES have user_id. These run BEFORE the
# direct deletes so the parent rows still exist for the subquery.
_INDIRECT_DELETE_QUERIES: list[tuple[str, str]] = [
    (
        "notebook_attendance",
        "DELETE FROM notebook_attendance WHERE entry_id IN "
        "(SELECT id FROM notebook_entries WHERE user_id = :uid)",
    ),
    (
        "video_annotations",
        "DELETE FROM video_annotations WHERE video_id IN "
        "(SELECT id FROM scouting_videos WHERE user_id = :uid)",
    ),
    (
        "video_clips",
        "DELETE FROM video_clips WHERE video_id IN "
        "(SELECT id FROM scouting_videos WHERE user_id = :uid)",
    ),
    (
        "clip_shares",
        "DELETE FROM clip_shares WHERE created_by = :uid",
    ),
    (
        "playlist_items",
        "DELETE FROM playlist_items WHERE playlist_id IN "
        "(SELECT id FROM clip_playlists WHERE user_id = :uid)",
    ),
]


async def flip_to_expired_and_schedule_purge(
    session: AsyncSession, user_id: int
) -> None:
    """Mark a trial-expired user as `expired` AND schedule
    `data_purge_at = NOW + 30 days`. Idempotent — `COALESCE` keeps the
    original purge date if one is already set, so re-checking doesn't
    push the purge further out."""
    purge_at = datetime.now(UTC) + timedelta(days=30)
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(
            subscription_plan="expired",
            # Keep existing data_purge_at if already set; else schedule fresh.
            # Idempotent across repeated calls.
            data_purge_at=func.coalesce(User.data_purge_at, purge_at),
        )
    )
    await session.execute(stmt)
    await session.flush()
    # NOTE on persistence: this flip is an out-of-band state-machine
    # transition. In v1.0-flask the same code path called `conn.commit()`
    # explicitly so the change persists regardless of how the surrounding
    # request ends. In our async stack, calling commit() mid-dependency
    # corrupts the greenlet context (cursor adapter raises MissingGreenlet
    # on the next operation). The pragmatic fix is to flush() here and let
    # FastAPI's `get_db` commit on successful request return. For the
    # 403-on-expiry path, the in-memory mutation in `maybe_flip_expired`
    # already triggers the SubscriptionError; the DB write follows as soon
    # as the request unwinds. If the request errors, the next request
    # re-detects the expiry and retries the flip — idempotent (COALESCE).


async def purge_user_data(session: AsyncSession, user_id: int) -> dict:
    """Delete all team-owned content for `user_id`. Returns `{deleted, failed,
    total}` for observability. Best-effort — a failure on one table is logged
    and we continue.

    **Caller is responsible for verifying `club_id IS NULL`** before calling
    this. Club members must never be purged. The `should_purge` helper
    enforces this check.
    """
    if not user_id:
        return {"deleted": {}, "failed": [], "total": 0}

    deleted: dict[str, int] = {}
    failed: list[str] = []

    # 1. Indirect (child) deletes first — parents still exist.
    for label, sql in _INDIRECT_DELETE_QUERIES:
        try:
            result = await session.execute(text(sql), {"uid": user_id})
            deleted[label] = result.rowcount or 0
        except SQLAlchemyError as e:
            logger.warning("[purge] indirect delete %s failed for uid=%s: %s",
                           label, user_id, e)
            failed.append(label)

    # 2. Direct deletes, in dependency-safe order.
    for table in _DIRECT_DELETE_TABLES:
        try:
            sql = f"DELETE FROM {table} WHERE user_id = :uid"
            result = await session.execute(text(sql), {"uid": user_id})
            deleted[table] = result.rowcount or 0
        except SQLAlchemyError as e:
            logger.warning("[purge] direct delete %s failed for uid=%s: %s",
                           table, user_id, e)
            failed.append(table)

    # 3. Clear data_purge_at so we don't re-run on next request.
    try:
        clear_stmt = update(User).where(User.id == user_id).values(data_purge_at=None)
        await session.execute(clear_stmt)
    except SQLAlchemyError as e:
        logger.warning("[purge] could not clear data_purge_at for uid=%s: %s",
                       user_id, e)

    await session.flush()

    total = sum(deleted.values())
    logger.info("[purge] uid=%s — deleted %d total rows across %d tables (failed: %s)",
                user_id, total, len(deleted), failed)

    return {"deleted": deleted, "failed": failed, "total": total}


def should_purge(user: User) -> bool:
    """Defensive check: True only if THIS user qualifies for data purge.

    Three conditions, ALL required:
      1. NOT a club member (club_id IS NULL)
      2. subscription_plan == 'expired'
      3. data_purge_at <= NOW (grace period over)

    If any condition fails, return False and DO NOT call purge_user_data."""
    if user.club_id is not None:
        return False
    if user.subscription_plan != "expired":
        return False
    if user.data_purge_at is None:
        return False
    purge_due = user.data_purge_at
    if purge_due.tzinfo is None:
        purge_due = purge_due.replace(tzinfo=UTC)
    return datetime.now(UTC) >= purge_due


def trial_days_left(trial_ends_at: str | None) -> int:
    """Whole days remaining in trial. Returns 0 within the final 24h.

    Used by both the API gate (auth.py) and the /api/me display
    aggregator (composite.py) so "0 days left" on screen aligns 1:1
    with "blocked at the gate" — keep these two callsites in sync."""
    if not trial_ends_at:
        return 0
    try:
        ends = datetime.fromisoformat(trial_ends_at)
    except (ValueError, TypeError):
        return 0
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=UTC)
    delta = ends - datetime.now(UTC)
    return max(0, delta.days)


def trial_hours_left(trial_ends_at: str | None) -> int:
    """Whole hours remaining in trial (for "Trial ends in 4h" UX when
    `trial_days_left` already shows 0). Returns 0 if expired or unset."""
    if not trial_ends_at:
        return 0
    try:
        ends = datetime.fromisoformat(trial_ends_at)
    except (ValueError, TypeError):
        return 0
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=UTC)
    delta = ends - datetime.now(UTC)
    return max(0, int(delta.total_seconds() // 3600))


async def maybe_flip_expired(session: AsyncSession, user: User) -> bool:
    """Detect trial-just-expired and convert to 'expired' state. Mirrors
    `backend/auth/decorators.py:225`. Returns True if the user was flipped
    (caller may want to refresh their in-memory `user` view).

    Club members never flip (they're exempt from the trial mechanic).

    "Expired" is reached when either (a) the wall clock is past
    `trial_ends_at` (v1 semantics, kept for parity) or (b) the displayed
    `trial_days_left` is 0 — i.e. less than 24h remain. (b) prevents the
    UX bug where users see "0 days left" but the gate hasn't fired yet,
    and aligns the persisted plan with what the user sees on screen.
    """
    if user.club_id is not None:
        return False
    if user.subscription_plan != "trial":
        return False
    if not user.trial_ends_at:
        return False

    try:
        ends = datetime.fromisoformat(user.trial_ends_at)
    except (ValueError, TypeError):
        return False
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    past_wall_clock = now > ends
    last_24h = (ends - now).days <= 0  # `.days` floors; 0 = within 24h
    if not past_wall_clock and not last_24h:
        return False

    await flip_to_expired_and_schedule_purge(session, user.id)
    user.subscription_plan = "expired"
    return True


__all__ = [
    "flip_to_expired_and_schedule_purge",
    "maybe_flip_expired",
    "purge_user_data",
    "should_purge",
    "trial_days_left",
    "trial_hours_left",
]
