"""Scheduled-messages cron — Phase 2.6c.

Picks up `Message` rows where status='SCHEDULED' AND scheduled_at <= now,
materializes their MessageDelivery rows (re-resolving recipients at send
time), and queues per-row dispatch. Mirrors the promote_draft_to_send
flow but driven by time, not a manual click.

Safety:
  - dry_run mode lists upcoming sends without mutating state.
  - MAX_PER_RUN cap so a backlog can't flood the dispatcher in one shot.
  - Workers stay idempotent: re-running on the same row is a no-op once
    status flips to SENDING.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import AsyncSessionLocal
from src.models.messages import Message, MessageDelivery
from src.models.user_organizations import UserOrganization
from src.services.message_send_worker import dispatch_message_delivery
from src.services.recipient_resolver import resolve_recipients

logger = logging.getLogger(__name__)

MAX_PER_RUN = 200


def _now():
    from datetime import UTC, datetime
    return datetime.now(UTC).replace(tzinfo=None)


async def find_due_messages(
    session: AsyncSession, *, limit: int = MAX_PER_RUN,
) -> list[Message]:
    now = _now()
    stmt = (
        select(Message)
        .where(Message.status == "SCHEDULED")
        .where(Message.scheduled_at <= now)
        .order_by(Message.scheduled_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _materialize_actor(session: AsyncSession, message: Message) -> UserOrganization | None:
    """Re-derive an actor for the resolver. We saved sent_by_user_id on the
    Message; pair it with the org to get a membership. If the user lost
    their membership in the meantime, we abort the send to avoid leaking
    cross-org recipients."""
    if message.sent_by_user_id is None:
        return None
    stmt = (
        select(UserOrganization)
        .where(UserOrganization.user_id == message.sent_by_user_id)
        .where(UserOrganization.organization_id == message.organization_id)
        .where(UserOrganization.status == "active")
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _promote_message(session: AsyncSession, message: Message) -> int:
    """Resolve recipients NOW (filter evaluates against current state) and
    create deliveries. Returns the count queued."""
    actor = await _materialize_actor(session, message)
    if actor is None:
        message.status = "CANCELLED"
        await session.flush()
        return 0
    pairs = await resolve_recipients(
        session,
        organization_id=message.organization_id,
        recipient_filter=message.recipient_filter or {"type": "all"},
        actor=actor,
    )
    if not pairs:
        # No recipients now → leave as SENT with zero deliveries, NOT cancel.
        # Same shape as a real send that resolved to zero people.
        message.status = "SENT"
        message.sent_at = _now()
        message.total_recipients = 0
        await session.flush()
        return 0

    message.status = "SENDING"
    message.total_recipients = len(pairs)
    message.sent_at = _now()

    deliveries: list[MessageDelivery] = []
    for player, contact in pairs:
        d = MessageDelivery(
            message_id=message.id,
            organization_id=message.organization_id,
            player_id=player.id,
            player_contact_id=contact.id,
            recipient_email=contact.parent_email,
            recipient_phone=contact.parent_phone_enc or "",
        )
        deliveries.append(d)
    session.add_all(deliveries)
    await session.flush()
    return len(deliveries)


async def run_scheduled(
    *,
    dry_run: bool = False,
    limit: int = MAX_PER_RUN,
) -> dict:
    """Top-level entry the cron endpoint calls."""
    async with AsyncSessionLocal() as session:
        due = await find_due_messages(session, limit=limit)

        if dry_run:
            return {
                "due_count": len(due),
                "dry_run": True,
                "preview": [
                    {
                        "message_id": m.id,
                        "organization_id": m.organization_id,
                        "subject": m.subject,
                        "scheduled_at": m.scheduled_at.isoformat() if m.scheduled_at else None,
                        "delivery_channels": m.delivery_channels,
                    }
                    for m in due
                ],
            }

        all_delivery_ids: list[int] = []
        promoted = 0
        for message in due:
            count = await _promote_message(session, message)
            if count > 0:
                promoted += 1
                # Re-fetch delivery ids inserted for this message so we can
                # queue them after commit.
                ids = list(
                    (await session.execute(
                        select(MessageDelivery.id).where(
                            MessageDelivery.message_id == message.id,
                            MessageDelivery.delivery_status == "PENDING",
                        )
                    )).scalars().all()
                )
                all_delivery_ids.extend(ids)
        await session.commit()

    # Fire per-delivery dispatch tasks outside the cron session.
    for did in all_delivery_ids:
        try:
            await dispatch_message_delivery(did)
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("[scheduled-msg] dispatch %d failed: %s", did, e)

    return {
        "due_count": len(due),
        "dry_run": False,
        "messages_promoted": promoted,
        "deliveries_queued": len(all_delivery_ids),
    }


__all__ = ["MAX_PER_RUN", "find_due_messages", "run_scheduled"]
