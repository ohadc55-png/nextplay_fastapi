"""Branches repository — org-scoped read/write over the `branches` table."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.branches import Branch
from src.repositories.org_scoped_repository import OrgScopedRepository


class BranchesRepository(OrgScopedRepository[Branch]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Branch)

    async def list_for_region(
        self, *, organization_id: int, region_id: int
    ) -> list[Branch]:
        """Branches belonging to a given region, scoped to the org for safety."""
        stmt = select(Branch).where(
            Branch.organization_id == organization_id,
            Branch.region_id == region_id,
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_name(self, *, organization_id: int, name: str) -> Branch | None:
        """Lookup branch by (org_id, name). Unique pair per uq_branches_org_name."""
        stmt = select(Branch).where(
            Branch.organization_id == organization_id,
            Branch.name == name,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["BranchesRepository"]
