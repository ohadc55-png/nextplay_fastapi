"""Read-side views over DocumentDelivery — Phase 2.5.

Powers the "who signed / who didn't" UI:
- `list_for_template_filtered()` — flat list with org/region/branch/team filter
  and stats grouped per status.
- `list_for_player()` — every delivery for a given player, used by the modal
  on /org/players.

All queries enforce `organization_id` and (for non-admin actors) narrow
to the actor's region/branch/coach scope so a coach can't peek into
other branches' deliveries.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.branches import Branch
from src.models.document_campaigns import DocumentCampaign
from src.models.document_deliveries import DocumentDelivery
from src.models.players import Player
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization


def _scope_to_actor(stmt, actor: UserOrganization):
    """Append region/branch/coach narrowing joins to a delivery select."""
    if actor.role == "region_manager":
        stmt = (
            stmt.join(Player, Player.id == DocumentDelivery.player_id)
            .join(TeamProfile, TeamProfile.id == Player.team_id)
            .join(Branch, Branch.id == TeamProfile.branch_id)
            .where(Branch.region_id == actor.region_id)
        )
    elif actor.role == "branch_manager":
        stmt = (
            stmt.join(Player, Player.id == DocumentDelivery.player_id)
            .join(TeamProfile, TeamProfile.id == Player.team_id)
            .where(TeamProfile.branch_id == actor.branch_id)
        )
    elif actor.role == "coach":
        stmt = (
            stmt.join(Player, Player.id == DocumentDelivery.player_id)
            .join(TeamProfile, TeamProfile.id == Player.team_id)
            .where(TeamProfile.user_id == actor.user_id)
        )
    return stmt


async def list_for_template_filtered(
    db: AsyncSession,
    *,
    template_id: int,
    organization_id: int,
    actor: UserOrganization,
    region_id: int | None = None,
    branch_id: int | None = None,
    team_id: int | None = None,
    status_filter: str | None = None,
) -> list[DocumentDelivery]:
    """All deliveries across all campaigns that used this template, narrowed
    by the actor's scope + optional region/branch/team/status filter."""
    stmt = (
        select(DocumentDelivery)
        .join(DocumentCampaign, DocumentCampaign.id == DocumentDelivery.campaign_id)
        .where(DocumentDelivery.organization_id == organization_id)
        .where(DocumentCampaign.template_id == template_id)
        .order_by(DocumentDelivery.created_at.desc())
    )
    stmt = _scope_to_actor(stmt, actor)

    if region_id is not None:
        # Already joined Player+TeamProfile if scoped by role; otherwise join here.
        if actor.role == "org_admin":
            stmt = (
                stmt.join(Player, Player.id == DocumentDelivery.player_id)
                .join(TeamProfile, TeamProfile.id == Player.team_id)
                .join(Branch, Branch.id == TeamProfile.branch_id)
                .where(Branch.region_id == region_id)
            )
        else:
            stmt = stmt.where(Branch.region_id == region_id)
    if branch_id is not None:
        if actor.role == "org_admin":
            stmt = (
                stmt.join(Player, Player.id == DocumentDelivery.player_id)
                .join(TeamProfile, TeamProfile.id == Player.team_id)
                .where(TeamProfile.branch_id == branch_id)
            )
        else:
            stmt = stmt.where(TeamProfile.branch_id == branch_id)
    if team_id is not None:
        if actor.role == "org_admin":
            stmt = stmt.join(Player, Player.id == DocumentDelivery.player_id).where(
                Player.team_id == team_id
            )
        else:
            stmt = stmt.where(Player.team_id == team_id)
    if status_filter:
        stmt = stmt.where(DocumentDelivery.document_status == status_filter)

    rows = (await db.execute(stmt)).scalars().unique().all()
    return list(rows)


def summarize(deliveries: list[DocumentDelivery]) -> dict:
    """Count breakdown for the stats header."""
    out = {
        "total": len(deliveries),
        "not_opened": 0,
        "opened": 0,
        "signed": 0,
        "expired": 0,
        "declined": 0,
        "delivery_failed": 0,
    }
    for d in deliveries:
        if d.delivery_status == "FAILED":
            out["delivery_failed"] += 1
        if d.document_status == "NOT_OPENED":
            out["not_opened"] += 1
        elif d.document_status == "OPENED" or d.document_status == "FILLED":
            out["opened"] += 1
        elif d.document_status == "SIGNED":
            out["signed"] += 1
        elif d.document_status == "EXPIRED":
            out["expired"] += 1
        elif d.document_status == "DECLINED":
            out["declined"] += 1
    return out


async def list_for_player(
    db: AsyncSession,
    *,
    player_id: int,
    organization_id: int,
) -> list[DocumentDelivery]:
    """Every delivery for one player, newest-first."""
    stmt = (
        select(DocumentDelivery)
        .where(DocumentDelivery.player_id == player_id)
        .where(DocumentDelivery.organization_id == organization_id)
        .order_by(DocumentDelivery.created_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def count_pending_for_players(
    db: AsyncSession,
    *,
    player_ids: list[int],
    organization_id: int,
) -> dict[int, int]:
    """Bulk lookup: returns {player_id: count_of_pending_deliveries}.

    "Pending" = anything not yet SIGNED, DECLINED, or EXPIRED. Used to paint
    the badge column on /org/players without N+1 queries.
    """
    if not player_ids:
        return {}
    stmt = (
        select(DocumentDelivery.player_id, DocumentDelivery.id)
        .where(DocumentDelivery.organization_id == organization_id)
        .where(DocumentDelivery.player_id.in_(player_ids))
        .where(DocumentDelivery.document_status.in_(("NOT_OPENED", "OPENED", "FILLED")))
    )
    rows = (await db.execute(stmt)).all()
    out: dict[int, int] = {}
    for player_id, _ in rows:
        out[player_id] = out.get(player_id, 0) + 1
    return out


__all__ = [
    "count_pending_for_players",
    "list_for_player",
    "list_for_template_filtered",
    "summarize",
]
