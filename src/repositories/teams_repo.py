"""Team profile repository.

Tenant model: each team is owned by exactly one coach via `user_id`. The
`active_team_id` column on `users` selects which team is "live" for that
coach. v1.0-flask functions covered:
- `get_user_teams(user_id)` → `list_for_user`
- `get_team_profile(team_id, user_id)` → `get_for_user`
- `delete_team` (with cascade-like cleanup) — implemented in service layer
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.teams import TeamProfile
from src.repositories.base_repository import BaseRepository


class TeamsRepository(BaseRepository[TeamProfile]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, TeamProfile)

    async def list_for_user(self, user_id: int) -> list[TeamProfile]:
        """Mirrors `db/__init__.py:141` — coach's teams ordered by latest edit."""
        stmt = (
            select(TeamProfile)
            .where(TeamProfile.user_id == user_id)
            .order_by(TeamProfile.updated_at.desc().nulls_last())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_for_user(self, team_id: int, user_id: int) -> TeamProfile | None:
        """Tenant-aware get: returns None unless the team belongs to the user.
        Mirrors the defensive pattern in `db/__init__.py:241-265`."""
        stmt = select(TeamProfile).where(
            TeamProfile.id == team_id, TeamProfile.user_id == user_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Phase 1.5 — org-scoped reads. ADDITIVE: never touches the coach-app
    # paths above. Every org query filters by organization_id so private
    # coach teams (organization_id IS NULL) cannot leak in.
    # ------------------------------------------------------------------

    async def list_for_org(
        self,
        organization_id: int | None,
        *,
        region_id: int | None = None,
        branch_id: int | None = None,
        coach_user_id: int | None = None,
    ) -> list[TeamProfile]:
        """Teams inside a specific org, optionally narrowed by region (via
        the team's branch), branch, or owning coach. Defensive: returns []
        when organization_id is None."""
        from src.models.branches import Branch

        if organization_id is None:
            return []
        stmt = select(TeamProfile).where(
            TeamProfile.organization_id == organization_id
        )
        if branch_id is not None:
            stmt = stmt.where(TeamProfile.branch_id == branch_id)
        if region_id is not None:
            # Teams whose branch lives in this region.
            stmt = stmt.where(
                TeamProfile.branch_id.in_(
                    select(Branch.id).where(Branch.region_id == region_id)
                )
            )
        if coach_user_id is not None:
            stmt = stmt.where(TeamProfile.user_id == coach_user_id)
        stmt = stmt.order_by(TeamProfile.team_name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_for_org(
        self, team_id: int, organization_id: int | None
    ) -> TeamProfile | None:
        """PK lookup scoped to organization. None when cross-org (route layer
        maps None → 404, per the 404-not-403 rule)."""
        if organization_id is None:
            return None
        stmt = select(TeamProfile).where(
            TeamProfile.id == team_id,
            TeamProfile.organization_id == organization_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["TeamsRepository"]
