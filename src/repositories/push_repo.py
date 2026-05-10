"""Web Push subscriptions + delivery log."""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.push import PushLog, PushSubscription
from src.repositories.base_repository import BaseRepository


class PushSubscriptionsRepository(BaseRepository[PushSubscription]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, PushSubscription)

    async def list_for_user(self, user_id: int) -> list[PushSubscription]:
        stmt = select(PushSubscription).where(PushSubscription.user_id == user_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_endpoint(self, endpoint: str) -> PushSubscription | None:
        """UNIQUE(endpoint) lookup. Used for upsert-on-resubscribe."""
        stmt = select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def delete_by_endpoint(self, endpoint: str) -> int:
        """Hard-delete a subscription. Returns rowcount. Used when web-push
        send returns 404/410 (subscription expired or unsubscribed)."""
        stmt = delete(PushSubscription).where(PushSubscription.endpoint == endpoint)
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0

    async def touch_last_used(self, sub_id: int) -> None:
        """Bump last_used_at after a successful push. Targeted UPDATE so we
        don't load + mutate the row."""
        from sqlalchemy import func as _func

        stmt = (
            update(PushSubscription)
            .where(PushSubscription.id == sub_id)
            .values(last_used_at=_func.now())
        )
        await self.session.execute(stmt)
        await self.session.flush()


class PushLogRepository(BaseRepository[PushLog]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, PushLog)

    async def has_recent_send(self, *, user_id: int, hours: int = 22) -> bool:
        """Daily-cap enforcement: did this user receive a push in the last N
        hours? Used by the scheduler to throttle. Mirrors v1.0-flask
        PUSH_INACTIVE_HOURS / daily-cap behavior."""
        from datetime import datetime, timedelta

        cutoff = datetime.utcnow() - timedelta(hours=hours)
        stmt = (
            select(PushLog.id)
            .where(
                PushLog.user_id == user_id,
                PushLog.status == "sent",
                PushLog.sent_at >= cutoff,
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None


__all__ = ["PushLogRepository", "PushSubscriptionsRepository"]
