"""OrgAuditLog repository — append-only.

Only `add()` is exposed. Updates and deletes are not — audit rows are
immutable. Postgres RLS additionally REVOKEs UPDATE/DELETE from the app
role for defense in depth.
"""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.org_audit import OrgAuditLog
from src.repositories.base_repository import BaseRepository


class OrgAuditRepository(BaseRepository[OrgAuditLog]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, OrgAuditLog)

    async def add(self, log: OrgAuditLog) -> OrgAuditLog:
        """Single allowed write path. Wraps `create` to make the append-only
        contract explicit at call sites."""
        return await self.create(log)

    async def list_for_org(
        self,
        organization_id: int | None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OrgAuditLog]:
        """Most-recent-first audit page. Defensive: returns [] if no org id."""
        if organization_id is None:
            return []
        stmt = (
            select(OrgAuditLog)
            .where(OrgAuditLog.organization_id == organization_id)
            .order_by(desc(OrgAuditLog.created_at))
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["OrgAuditRepository"]
