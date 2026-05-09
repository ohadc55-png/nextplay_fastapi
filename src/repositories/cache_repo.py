"""IP geolocation cache."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.cache import IpGeoCache
from src.repositories.base_repository import BaseRepository


class IpGeoCacheRepository(BaseRepository[IpGeoCache]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, IpGeoCache)

    async def get_for_ip(self, ip: str) -> IpGeoCache | None:
        """PK is the IP itself. None means "never resolved" — caller hits
        the geolocation API and inserts the result."""
        stmt = select(IpGeoCache).where(IpGeoCache.ip == ip)
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["IpGeoCacheRepository"]
