"""Plays + play_shares repositories."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.plays import Play, PlayShare
from src.repositories.base_repository import BaseRepository, TeamScopedRepository


class PlaysRepository(TeamScopedRepository[Play]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Play)


class PlaySharesRepository(BaseRepository[PlayShare]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, PlayShare)

    async def get_by_token(self, token: str) -> PlayShare | None:
        """Public read; no auth on the share-link route."""
        stmt = select(PlayShare).where(PlayShare.token == token)
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["PlaysRepository", "PlaySharesRepository"]
