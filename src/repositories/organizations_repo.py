"""Organizations repository — System Admin CRUD over the `organizations` table.

System Admin is the only actor that creates/deletes orgs. Org Admins can
read their own org's row but cannot create new ones. Soft-delete only:
clearing `organizations.deleted_at` "archives" the org without losing audit
history.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.organizations import Organization
from src.repositories.base_repository import BaseRepository


class OrganizationsRepository(BaseRepository[Organization]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Organization)

    async def get_by_slug(self, slug: str) -> Organization | None:
        """Find by URL-safe slug; returns None if missing or soft-deleted."""
        stmt = select(Organization).where(
            Organization.slug == slug,
            Organization.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_active(self) -> list[Organization]:
        """All non-archived orgs, ordered by name. System-admin only —
        regular org members get their own org via the membership pivot."""
        stmt = (
            select(Organization)
            .where(Organization.deleted_at.is_(None))
            .order_by(Organization.name)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def soft_delete(self, org_id: int) -> bool:
        """Stamp deleted_at without removing the row (preserves audit FKs).
        Returns True if the org existed and was not already archived."""
        org = await self.get(org_id)
        if not org or org.deleted_at is not None:
            return False
        org.deleted_at = datetime.utcnow()
        await self.session.flush()
        return True


__all__ = ["OrganizationsRepository"]
