"""Per-delivery dispatch for Messages — Phase 2.5.

Simpler than the document send worker: no token, no signing URL, no
template-related lookups. Resolves the placeholder values per
recipient, sends through the first available channel, flips status.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import AsyncSessionLocal
from src.models.messages import Message, MessageDelivery
from src.models.organizations import Organization
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.services.email import send_email_now
from src.services.email_templates import render as render_email
from src.services.message_service import build_placeholder_values, render_placeholders
from src.services.sms import get_sms_provider

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _send_sms_for(
    delivery: MessageDelivery, message: Message, values: dict[str, str]
) -> bool:
    if not delivery.recipient_phone:
        return False
    body = render_placeholders(message.body, values=values)
    provider = get_sms_provider()
    result = await provider.send(delivery.recipient_phone, body)
    return bool(result.success)


async def _send_email_for(
    session: AsyncSession,
    delivery: MessageDelivery,
    message: Message,
    values: dict[str, str],
) -> bool:
    if not delivery.recipient_email:
        return False
    rendered_subject = render_placeholders(message.subject, values=values)
    rendered_body = render_placeholders(message.body, values=values)
    subject, html, text = render_email(
        "message_notification",
        language="he",
        context={
            "recipient_name": values.get("parent_name") or "הורה",
            "subject": rendered_subject,
            "body_text": rendered_body,
            "organization_name": values.get("org_name") or "הארגון",
        },
    )
    result = await send_email_now(
        session,
        user_id=None,
        to_email=delivery.recipient_email,
        subject=subject,
        html=html,
        text=text,
        template="message.notify",
        language="he",
        kind="transactional",
    )
    return bool(result and result.get("status") == "sent")


async def dispatch_message_delivery(delivery_id: int) -> None:
    """Background task entry point. Never raises — errors stored in failed_reason."""
    async with AsyncSessionLocal() as session:
        delivery = await session.get(MessageDelivery, delivery_id)
        if delivery is None:
            logger.warning("[msg-worker] delivery %d not found", delivery_id)
            return
        if delivery.delivery_status not in ("PENDING",):
            return
        message = await session.get(Message, delivery.message_id)
        if message is None:
            delivery.delivery_status = "FAILED"
            delivery.failed_reason = "message not found"
            await session.commit()
            return

        player = await session.get(Player, delivery.player_id)
        contact = (
            await session.get(PlayerContact, delivery.player_contact_id)
            if delivery.player_contact_id
            else None
        )
        org = await session.get(Organization, delivery.organization_id)
        if player is None:
            delivery.delivery_status = "FAILED"
            delivery.failed_reason = "player not found"
            await session.commit()
            return

        values = await build_placeholder_values(
            session, player=player, contact=contact, org=org
        )

        channels = list(message.delivery_channels or ["sms"])
        last_error: str | None = None
        for channel in channels:
            try:
                if channel == "sms":
                    ok = await _send_sms_for(delivery, message, values)
                    if ok:
                        delivery.delivery_status = "SENT"
                        delivery.channel_used = "SMS"
                        delivery.sent_at = _now()
                        message.total_delivered = (message.total_delivered or 0) + 1
                        last_error = None
                        break
                elif channel == "email":
                    ok = await _send_email_for(session, delivery, message, values)
                    if ok:
                        delivery.delivery_status = "SENT"
                        delivery.channel_used = "EMAIL"
                        delivery.sent_at = _now()
                        message.total_delivered = (message.total_delivered or 0) + 1
                        last_error = None
                        break
            except Exception as e:  # pragma: no cover — defensive
                last_error = f"{channel}: {e}"

        if delivery.delivery_status == "PENDING":
            delivery.delivery_status = "FAILED"
            delivery.failed_reason = last_error or "no working channel"
            message.total_failed = (message.total_failed or 0) + 1

        # Flip SENDING → SENT when all deliveries are accounted for.
        pending_remaining = (
            (message.total_recipients or 0)
            - (message.total_delivered or 0)
            - (message.total_failed or 0)
        )
        if pending_remaining <= 0 and message.status == "SENDING":
            message.status = "SENT"

        await session.commit()


__all__ = ["dispatch_message_delivery"]
