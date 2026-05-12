"""Campaign creation — Phase 2.4.

Recipient resolution moved to `src.services.recipient_resolver` so the
messaging module (2.5) can share the same scoping rules. This file now
only owns the "create a campaign + N deliveries in one transaction"
flow.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document_campaigns import DocumentCampaign
from src.models.document_deliveries import DocumentDelivery
from src.models.document_templates import DocumentTemplate
from src.models.user_organizations import UserOrganization
from src.services.org_audit_service import log_org_action
from src.services.recipient_resolver import (
    VALID_FILTER_TYPES,
    count_recipients,
    resolve_recipients,
)

logger = logging.getLogger(__name__)


async def create_campaign_with_deliveries(
    db: AsyncSession,
    *,
    template: DocumentTemplate,
    title: str,
    recipient_filter: dict,
    delivery_channels: list[str],
    actor: UserOrganization,
    actor_email: str | None,
    expires_in_days: int | None = None,
    reminder_after_days: int | None = 7,
    request=None,
) -> tuple[DocumentCampaign, list[DocumentDelivery]]:
    """Create a campaign + one DocumentDelivery per resolved recipient,
    in one transaction. The deliveries start as PENDING; the send worker
    flips them to SENT_SMS / SENT_EMAIL.

    PII access (decrypted phone for snapshot) is audit-logged ONCE per
    campaign with `extra={"player_count": N}`. Granular per-row decryption
    logs are deferred to Part B.
    """
    pairs = await resolve_recipients(
        db,
        organization_id=template.organization_id,
        recipient_filter=recipient_filter,
        actor=actor,
    )
    if not pairs:
        raise ValueError("No recipients matched the filter.")

    now = datetime.now(UTC).replace(tzinfo=None)
    expiry_days = int(expires_in_days or template.default_expiry_days or 30)
    expires_at = now + timedelta(days=expiry_days)

    campaign = DocumentCampaign(
        organization_id=template.organization_id,
        template_id=template.id,
        title=title.strip(),
        recipient_filter=recipient_filter,
        delivery_channels=list(delivery_channels),
        expires_at=expires_at,
        reminder_after_days=reminder_after_days,
        status="SENDING",
        total_recipients=len(pairs),
        created_by_user_id=actor.user_id,
        sent_at=now,
    )
    db.add(campaign)
    await db.flush()

    deliveries: list[DocumentDelivery] = []
    for player, contact in pairs:
        token = uuid.uuid4().hex  # 32 chars
        # PlayerContact.parent_phone_enc auto-decrypts via EncryptedText.
        decrypted_phone = contact.parent_phone_enc or ""
        d = DocumentDelivery(
            campaign_id=campaign.id,
            organization_id=template.organization_id,
            player_id=player.id,
            player_contact_id=contact.id,
            recipient_name=contact.parent_name or player.name or "הורה",
            recipient_email=contact.parent_email,
            recipient_phone=decrypted_phone,
            unique_token=token,
            expires_at=expires_at,
        )
        deliveries.append(d)
    db.add_all(deliveries)
    await db.flush()

    await log_org_action(
        db,
        organization_id=template.organization_id,
        actor_user_id=actor.user_id,
        actor_email=actor_email,
        action="player.contact.read",
        target_type="document_campaign",
        target_id=campaign.id,
        request=request,
        extra={
            "reason": "document_send",
            "player_count": len(pairs),
            "channels": list(delivery_channels),
        },
    )
    await log_org_action(
        db,
        organization_id=template.organization_id,
        actor_user_id=actor.user_id,
        actor_email=actor_email,
        action="document.campaign.create",
        target_type="document_campaign",
        target_id=campaign.id,
        request=request,
        extra={
            "template_id": template.id,
            "filter_type": recipient_filter.get("type"),
            "recipients": len(pairs),
        },
    )

    return campaign, deliveries


__all__ = [
    "VALID_FILTER_TYPES",
    "count_recipients",
    "create_campaign_with_deliveries",
    "resolve_recipients",
]
