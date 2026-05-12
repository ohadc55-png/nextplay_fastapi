"""Message creation + draft management — Phase 2.5.

The messaging module is the simpler twin of document campaigns: just a
subject + body delivered as SMS or email, no signing, no OTP, no PDF.

Supports drafts (`status="DRAFT"`) — saved but not sent. The send path
flips to SENDING → SENT, creating one MessageDelivery per resolved
recipient.

Placeholders: `{{parent_name}}`, `{{player_name}}`, `{{team_name}}`,
`{{branch_name}}`, `{{org_name}}`. Rendered per-delivery at send time
inside the worker (each parent gets a body with their own values).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.messages import Message, MessageDelivery
from src.models.organizations import Organization
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.user_organizations import UserOrganization
from src.services.org_audit_service import log_org_action
from src.services.recipient_resolver import resolve_recipients

logger = logging.getLogger(__name__)


PLACEHOLDER_KEYS = (
    "parent_name",
    "player_name",
    "team_name",
    "branch_name",
    "org_name",
)


def render_placeholders(text: str, *, values: dict[str, str]) -> str:
    """Replace `{{key}}` tokens with values. Unknown keys are kept as-is
    so authors notice a typo (vs silently disappearing)."""
    out = text
    for key in PLACEHOLDER_KEYS:
        token = "{{" + key + "}}"
        if token in out:
            out = out.replace(token, values.get(key, "") or "")
    return out


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def create_draft(
    db: AsyncSession,
    *,
    organization_id: int,
    subject: str,
    body: str,
    recipient_filter: dict,
    delivery_channels: list[str],
    actor: UserOrganization,
) -> Message:
    """Save a DRAFT message. No deliveries created yet — recipients are
    re-resolved at send time so the filter stays live until then."""
    msg = Message(
        organization_id=organization_id,
        subject=subject.strip(),
        body=body,
        recipient_filter=recipient_filter,
        delivery_channels=list(delivery_channels),
        status="DRAFT",
        sent_by_user_id=actor.user_id,
    )
    db.add(msg)
    await db.flush()
    return msg


async def update_draft(
    db: AsyncSession,
    *,
    message: Message,
    subject: str | None = None,
    body: str | None = None,
    recipient_filter: dict | None = None,
    delivery_channels: list[str] | None = None,
) -> Message:
    """Edit a DRAFT in place. Refuses to touch anything not DRAFT."""
    if message.status != "DRAFT":
        raise ValueError("Only DRAFT messages can be edited.")
    if subject is not None:
        message.subject = subject.strip()
    if body is not None:
        message.body = body
    if recipient_filter is not None:
        message.recipient_filter = recipient_filter
    if delivery_channels is not None:
        message.delivery_channels = list(delivery_channels)
    await db.flush()
    return message


async def create_message_with_deliveries(
    db: AsyncSession,
    *,
    organization_id: int,
    subject: str,
    body: str,
    recipient_filter: dict,
    delivery_channels: list[str],
    actor: UserOrganization,
    actor_email: str | None,
    request=None,
) -> tuple[Message, list[MessageDelivery]]:
    """Materialize a message + its deliveries in one transaction. PII access
    (parent_phone decrypt) is audit-logged once with the recipient count."""
    pairs = await resolve_recipients(
        db,
        organization_id=organization_id,
        recipient_filter=recipient_filter,
        actor=actor,
    )
    if not pairs:
        raise ValueError("No recipients matched the filter.")

    now = _now()
    msg = Message(
        organization_id=organization_id,
        subject=subject.strip(),
        body=body,
        recipient_filter=recipient_filter,
        delivery_channels=list(delivery_channels),
        status="SENDING",
        total_recipients=len(pairs),
        sent_by_user_id=actor.user_id,
        sent_at=now,
    )
    db.add(msg)
    await db.flush()

    deliveries: list[MessageDelivery] = []
    for player, contact in pairs:
        decrypted_phone = contact.parent_phone_enc or ""
        d = MessageDelivery(
            message_id=msg.id,
            organization_id=organization_id,
            player_id=player.id,
            player_contact_id=contact.id,
            recipient_email=contact.parent_email,
            recipient_phone=decrypted_phone,
        )
        deliveries.append(d)
    db.add_all(deliveries)
    await db.flush()

    await log_org_action(
        db,
        organization_id=organization_id,
        actor_user_id=actor.user_id,
        actor_email=actor_email,
        action="player.contact.read",
        target_type="message",
        target_id=msg.id,
        request=request,
        extra={
            "reason": "message_send",
            "player_count": len(pairs),
            "channels": list(delivery_channels),
        },
    )
    await log_org_action(
        db,
        organization_id=organization_id,
        actor_user_id=actor.user_id,
        actor_email=actor_email,
        action="message.create",
        target_type="message",
        target_id=msg.id,
        request=request,
        extra={
            "filter_type": recipient_filter.get("type"),
            "recipients": len(pairs),
        },
    )

    return msg, deliveries


async def promote_draft_to_send(
    db: AsyncSession,
    *,
    message: Message,
    actor: UserOrganization,
    actor_email: str | None,
    request=None,
) -> tuple[Message, list[MessageDelivery]]:
    """Resolve recipients now, create deliveries, flip status DRAFT → SENDING."""
    if message.status != "DRAFT":
        raise ValueError("Message is not a draft.")
    pairs = await resolve_recipients(
        db,
        organization_id=message.organization_id,
        recipient_filter=message.recipient_filter or {"type": "all"},
        actor=actor,
    )
    if not pairs:
        raise ValueError("No recipients matched the filter.")

    now = _now()
    message.status = "SENDING"
    message.total_recipients = len(pairs)
    message.sent_at = now

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
    db.add_all(deliveries)
    await db.flush()

    await log_org_action(
        db,
        organization_id=message.organization_id,
        actor_user_id=actor.user_id,
        actor_email=actor_email,
        action="player.contact.read",
        target_type="message",
        target_id=message.id,
        request=request,
        extra={
            "reason": "message_send",
            "player_count": len(pairs),
            "channels": list(message.delivery_channels or []),
        },
    )
    await log_org_action(
        db,
        organization_id=message.organization_id,
        actor_user_id=actor.user_id,
        actor_email=actor_email,
        action="message.send",
        target_type="message",
        target_id=message.id,
        request=request,
        extra={"recipients": len(pairs)},
    )
    return message, deliveries


async def build_placeholder_values(
    db: AsyncSession,
    *,
    player: Player,
    contact: PlayerContact | None,
    org: Organization | None,
) -> dict[str, str]:
    """Per-delivery placeholder map. Branch/team are loaded best-effort
    so a missing relationship doesn't fail the whole send."""
    from src.models.branches import Branch
    from src.models.teams import TeamProfile

    parent_name = (contact.parent_name if contact else None) or "הורה"
    player_name = player.name or ""
    team_name = ""
    branch_name = ""
    if player.team_id:
        team = await db.get(TeamProfile, player.team_id)
        if team is not None:
            team_name = team.team_name or ""
            if team.branch_id:
                branch = await db.get(Branch, team.branch_id)
                if branch is not None:
                    branch_name = branch.name or ""
    org_name = (org.name if org else "") or ""
    return {
        "parent_name": parent_name,
        "player_name": player_name,
        "team_name": team_name,
        "branch_name": branch_name,
        "org_name": org_name,
    }


__all__ = [
    "PLACEHOLDER_KEYS",
    "build_placeholder_values",
    "create_draft",
    "create_message_with_deliveries",
    "promote_draft_to_send",
    "render_placeholders",
    "update_draft",
]
