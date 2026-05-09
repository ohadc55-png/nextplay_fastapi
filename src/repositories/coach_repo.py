"""Coach personalization (preferences + feedback)."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.coach import CoachPreference, Feedback
from src.repositories.base_repository import BaseRepository


class CoachPreferencesRepository(BaseRepository[CoachPreference]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, CoachPreference)

    async def get_for_user(self, user_id: int) -> CoachPreference | None:
        """One row per user (UNIQUE(user_id))."""
        stmt = select(CoachPreference).where(CoachPreference.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert(self, *, user_id: int, **fields) -> None:
        """Update existing row or insert a new one with the given fields.
        UPDATE-first / INSERT-fallback so it's dialect-agnostic."""
        stmt = update(CoachPreference).where(CoachPreference.user_id == user_id).values(**fields)
        result = await self.session.execute(stmt)
        if (result.rowcount or 0) > 0:
            await self.session.flush()
            return
        new_row = CoachPreference(user_id=user_id, **fields)
        self.session.add(new_row)
        await self.session.flush()


class FeedbackRepository(BaseRepository[Feedback]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Feedback)

    async def list_for_user(
        self, *, user_id: int, agent_key: str | None = None, limit: int = 50
    ) -> list[Feedback]:
        """Most recent N feedback rows. Optionally filter by agent."""
        stmt = select(Feedback).where(Feedback.user_id == user_id)
        if agent_key is not None:
            stmt = stmt.where(Feedback.agent_key == agent_key)
        stmt = stmt.order_by(Feedback.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["CoachPreferencesRepository", "FeedbackRepository"]
