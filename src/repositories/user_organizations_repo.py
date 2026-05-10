"""UserOrganizations repository — the membership pivot.

The pivot is the source of truth for "who can do what in which org". Every
`/org/*` request load the membership row to determine the active role and
scope (region/branch).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.user_organizations import UserOrganization
from src.repositories.base_repository import BaseRepository


class UserOrganizationsRepository(BaseRepository[UserOrganization]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, UserOrganization)

    async def list_for_user(
        self, user_id: int | None, *, only_active: bool = True
    ) -> list[UserOrganization]:
        """All memberships for a user. Defensive: returns [] if user_id is None.

        `only_active=True` filters to status='active' rows — used for login
        flows. Pass False to include suspended memberships (for admin views)."""
        if user_id is None:
            return []
        stmt = select(UserOrganization).where(UserOrganization.user_id == user_id)
        if only_active:
            stmt = stmt.where(UserOrganization.status == "active")
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_org(
        self, organization_id: int | None, *, only_active: bool = True
    ) -> list[UserOrganization]:
        """All members of an org. Defensive: returns [] if organization_id is None."""
        if organization_id is None:
            return []
        stmt = select(UserOrganization).where(
            UserOrganization.organization_id == organization_id
        )
        if only_active:
            stmt = stmt.where(UserOrganization.status == "active")
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_active(
        self, *, user_id: int, organization_id: int, role: str
    ) -> UserOrganization | None:
        """Resolve the active membership row by (user_id, org_id, role).
        Returns None if missing or status != 'active'. The caller translates
        None to 404 (the `404-not-403` rule for cross-org access)."""
        stmt = select(UserOrganization).where(
            UserOrganization.user_id == user_id,
            UserOrganization.organization_id == organization_id,
            UserOrganization.role == role,
            UserOrganization.status == "active",
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["UserOrganizationsRepository"]
