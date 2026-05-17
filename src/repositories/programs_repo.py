"""Programs repository — org-scoped read/write over the `programs` table.

Programs are the second tier of the active hierarchy:
  Organization -> Program -> Region -> Team

A program_manager membership row points at one program; their queries
funnel through this repo's `list_for_org` + `get_for_org` (which guarantee
the active org never leaks to siblings) plus the helpers below for
slicing by program.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.programs import Program
from src.repositories.org_scoped_repository import OrgScopedRepository


class ProgramsRepository(OrgScopedRepository[Program]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Program)

    async def get_by_name(
        self, *, organization_id: int, name: str
    ) -> Program | None:
        """Lookup program by (org_id, name). Unique pair per uq_programs_org_name."""
        stmt = select(Program).where(
            Program.organization_id == organization_id,
            Program.name == name,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_slug(
        self, *, organization_id: int, slug: str
    ) -> Program | None:
        stmt = select(Program).where(
            Program.organization_id == organization_id,
            Program.slug == slug,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["ProgramsRepository"]
