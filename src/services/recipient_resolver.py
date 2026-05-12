"""Shared recipient resolver — used by Documents (2.4) and Messages (2.5).

Both the document signing flow and the standalone messaging module pick
recipients with the same shape:

    {"type": "all"|"region"|"branch"|"team"|"specific_players",
     "region_id": int|None,
     "branch_id": int|None,
     "team_ids": [int, ...],
     "player_ids": [int, ...]}

Resolver scopes by `organization_id` and (for non-org_admin actors)
narrows further to the actor's region/branch/coach scope. Cross-scope
filter values are silently dropped — better to under-deliver than to
leak across tenants.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.branches import Branch
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization

VALID_FILTER_TYPES = {"all", "region", "branch", "team", "specific_players"}


async def resolve_recipients(
    db: AsyncSession,
    *,
    organization_id: int,
    recipient_filter: dict,
    actor: UserOrganization,
) -> list[tuple[Player, PlayerContact]]:
    """Return (player, contact) pairs for the filter, scoped to actor's role.

    Players without a PlayerContact row are skipped — there'd be no way to
    reach them anyway.
    """
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
    """Live count for modal previews. Same scoping as the actual send."""
    pairs = await resolve_recipients(
        db,
        organization_id=organization_id,
        recipient_filter=recipient_filter,
        actor=actor,
    )
    return len(pairs)


__all__ = [
    "VALID_FILTER_TYPES",
    "count_recipients",
    "resolve_recipients",
]
