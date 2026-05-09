"""Sales inquiries repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.inquiries import SalesInquiry
from src.repositories.base_repository import BaseRepository


class SalesInquiriesRepository(BaseRepository[SalesInquiry]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, SalesInquiry)

    async def list_open(self, *, limit: int = 100) -> list[SalesInquiry]:
        """Inquiries not yet contacted/converted/lost."""
        stmt = (
            select(SalesInquiry)
            .where(SalesInquiry.status == "new")
            .order_by(SalesInquiry.created_at.desc().nulls_last())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["SalesInquiriesRepository"]
