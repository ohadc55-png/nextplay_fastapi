"""Regions repository — org-scoped read/write over the `regions` table."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.regions import Region
from src.repositories.org_scoped_repository import OrgScopedRepository


class RegionsRepository(OrgScopedRepository[Region]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Region)

    async def get_by_name(self, *, organization_id: int, name: str) -> Region | None:
        """Lookup region by (org_id, name). Unique pair per uq_regions_org_name."""
        stmt = select(Region).where(
            Region.organization_id == organization_id,
            Region.name == name,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["RegionsRepository"]
