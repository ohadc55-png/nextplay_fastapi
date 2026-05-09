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


__all__ = ["TeamsRepository"]
