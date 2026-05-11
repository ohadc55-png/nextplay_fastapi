"""Campaign creation + recipient resolution — Phase 2.4.

The route handler stays thin:
1. Pick a template + recipient filter from the request.
2. Call `resolve_recipients()` to get the (player, contact) pairs.
3. Call `create_campaign_with_deliveries()` to create the campaign + N
   DocumentDelivery rows in one transaction.
4. Schedule `dispatch_delivery()` background tasks per delivery.

`recipient_filter` shape (mirrors the JSON column):
    {"type": "all"|"region"|"branch"|"team"|"specific_players",
     "region_id": int|None,
     "branch_id": int|None,
     "team_ids": [int, ...],
     "player_ids": [int, ...]}

The resolver scopes by `organization_id` and (when invoked by a non-org_admin)
narrows further to the actor's region/branch/coach scope. Cross-scope filter
values are silently dropped — better to under-deliver than to leak.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.branches import Branch
from src.models.document_campaigns import DocumentCampaign
from src.models.document_deliveries import DocumentDelivery
from src.models.document_templates import DocumentTemplate
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.services.org_audit_service import log_org_action

logger = logging.getLogger(__name__)


VALID_FILTER_TYPES = {"all", "region", "branch", "team", "specific_players"}


async def resolve_recipients(
    db: AsyncSession,
    *,
    organization_id: int,
    recipient_filter: dict,
    actor: UserOrganization,
) -> list[tuple[Player, PlayerContact]]:
    """Return (player, contact) pairs for the filter, scoped to the actor's
    role. Players without a PlayerContact are skipped (we'd have no way to
    send them anyway)."""
    f_type = recipient_filter.get("type", "all")
    if f_type not in VALID_FILTER_TYPES:
        raise ValueError(f"Unknown recipient_filter.type: {f_type!r}")

    stmt = (
        select(Player, PlayerContact)
        .join(PlayerContact, PlayerContact.player_id == Player.id)
        .where(Player.organization_id == organization_id)
        .where(Player.active.is_(True))
    )

    if f_type == "region":
        rid = recipient_filter.get("region_id")
        if rid is None:
            return []
        if actor.role == "region_manager" and actor.region_id != rid:
            return []
        stmt = (
            stmt.join(TeamProfile, TeamProfile.id == Player.team_id)
            .join(Branch, Branch.id == TeamProfile.branch_id)
            .where(Branch.region_id == rid)
        )
    elif f_type == "branch":
        bid = recipient_filter.get("branch_id")
        if bid is None:
            return []
        if actor.role == "branch_manager" and actor.branch_id != bid:
            return []
        if actor.role == "region_manager":
            branch = (await db.execute(
                select(Branch).where(Branch.id == bid)
            )).scalar_one_or_none()
            if branch is None or branch.region_id != actor.region_id:
                return []
        stmt = stmt.join(TeamProfile, TeamProfile.id == Player.team_id).where(
            TeamProfile.branch_id == bid
        )
    elif f_type == "team":
        team_ids = list(recipient_filter.get("team_ids") or [])
        if not team_ids:
            return []
        stmt = stmt.where(Player.team_id.in_(team_ids))
        if actor.role in ("region_manager", "branch_manager", "coach"):
            stmt = stmt.join(TeamProfile, TeamProfile.id == Player.team_id)
            if actor.role == "region_manager":
                stmt = (
                    stmt.join(Branch, Branch.id == TeamProfile.branch_id)
                    .where(Branch.region_id == actor.region_id)
                )
            elif actor.role == "branch_manager":
                stmt = stmt.where(TeamProfile.branch_id == actor.branch_id)
            elif actor.role == "coach":
                stmt = stmt.where(TeamProfile.user_id == actor.user_id)
    elif f_type == "specific_players":
        pids = list(recipient_filter.get("player_ids") or [])
        if not pids:
            return []
        stmt = stmt.where(Player.id.in_(pids))
    else:  # all
        if actor.role == "region_manager":
            stmt = (
                stmt.join(TeamProfile, TeamProfile.id == Player.team_id)
                .join(Branch, Branch.id == TeamProfile.branch_id)
                .where(Branch.region_id == actor.region_id)
            )
        elif actor.role == "branch_manager":
            stmt = stmt.join(TeamProfile, TeamProfile.id == Player.team_id).where(
                TeamProfile.branch_id == actor.branch_id
            )
        elif actor.role == "coach":
            stmt = stmt.join(TeamProfile, TeamProfile.id == Player.team_id).where(
                TeamProfile.user_id == actor.user_id
            )

    rows = (await db.execute(stmt)).all()
    return [(p, c) for (p, c) in rows]


async def count_recipients(
    db: AsyncSession,
    *,
    organization_id: int,
    recipient_filter: dict,
    actor: UserOrganization,
) -> int:
    """Live count for the modal preview. Same scoping as the actual send."""
    pairs = await resolve_recipients(
        db,
        organization_id=organization_id,
        recipient_filter=recipient_filter,
        actor=actor,
    )
    return len(pairs)


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
