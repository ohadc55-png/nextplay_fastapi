"""Analytics + observability repositories."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.analytics import ApiUsageLog, OnboardingEvent, PageView, ResearchUrlLog
from src.repositories.base_repository import BaseRepository


class PageViewsRepository(BaseRepository[PageView]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, PageView)


class OnboardingEventsRepository(BaseRepository[OnboardingEvent]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, OnboardingEvent)

    async def has_event(
        self, *, user_id: int, team_id: int, event: str
    ) -> bool:
        """Was this milestone reached for (user, team) yet? Mirrors the
        idempotency UNIQUE(user_id, team_id, event)."""
        stmt = (
            select(OnboardingEvent.id)
            .where(
                OnboardingEvent.user_id == user_id,
                OnboardingEvent.team_id == team_id,
                OnboardingEvent.event == event,
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None


class ApiUsageLogsRepository(BaseRepository[ApiUsageLog]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, ApiUsageLog)

    async def total_cost_for_user(self, user_id: int) -> float:
        """Sum cost_usd for a user. Powers the per-coach spend display."""
        stmt = select(func.coalesce(func.sum(ApiUsageLog.cost_usd), 0.0)).where(
            ApiUsageLog.user_id == user_id
        )
        return float((await self.session.execute(stmt)).scalar() or 0.0)


class ResearchUrlLogRepository(BaseRepository[ResearchUrlLog]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, ResearchUrlLog)

    async def list_recent(self, *, limit: int = 200) -> list[ResearchUrlLog]:
        stmt = (
            select(ResearchUrlLog)
            .order_by(ResearchUrlLog.used_at.desc().nulls_last())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = [
    "ApiUsageLogsRepository",
    "OnboardingEventsRepository",
    "PageViewsRepository",
    "ResearchUrlLogRepository",
]
