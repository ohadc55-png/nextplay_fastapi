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

    async def list_with_user_data(
        self,
        organization_id: int | None,
        *,
        role: str | None = None,
        program_id: int | None = None,
        region_id: int | None = None,
        branch_id: int | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> list[dict]:
        """Members + user.email + user.display_name in one query.

        Used by /org/api/users (Phase 1.4) to populate the people table.
        Defensive: returns [] if organization_id is None.

        `program_id` filter is also *inclusive*: matches members whose
        membership pins program_id directly OR whose region belongs to
        that program (region_manager + coaches scoped to a region). Without
        this, a region_manager in a program would be invisible when the PM
        filters by `program_id` — counter-intuitive.

        `region_id` filter is *inclusive*: matches members whose membership
        either pins region_id directly (region_manager / branch_manager) OR
        whose branch belongs to that region (coaches scoped to a branch).
        """
        from sqlalchemy import or_  # local import — keeps the file's top

        from src.models.branches import Branch
        from src.models.users import User

        if organization_id is None:
            return []
        stmt = (
            select(UserOrganization, User)
            .join(User, User.id == UserOrganization.user_id)
            .where(UserOrganization.organization_id == organization_id)
        )
        if role is not None:
            stmt = stmt.where(UserOrganization.role == role)
        if program_id is not None:
            # Phase 12 — regions are shared across programs, so "members in
            # program X" means only those pinned via UserOrganization.program_id.
            # The legacy union with regions_in_program (Region.program_id == X)
            # is gone — that mapping is now always NULL.
            stmt = stmt.where(UserOrganization.program_id == program_id)
        if region_id is not None:
            branches_in_region = (
                select(Branch.id).where(
                    Branch.organization_id == organization_id,
                    Branch.region_id == region_id,
                )
            )
            stmt = stmt.where(or_(
                UserOrganization.region_id == region_id,
                UserOrganization.branch_id.in_(branches_in_region),
            ))
        if branch_id is not None:
            stmt = stmt.where(UserOrganization.branch_id == branch_id)
        if statuses is not None:
            stmt = stmt.where(UserOrganization.status.in_(statuses))
        else:
            stmt = stmt.where(UserOrganization.status == "active")

        rows = (await self.session.execute(stmt)).all()
        out: list[dict] = []
        for uo, user in rows:
            out.append({
                "membership_id": uo.id,
                "user_id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "role": uo.role,
                "program_id": getattr(uo, "program_id", None),
                "region_id": uo.region_id,
                "branch_id": uo.branch_id,
                "status": uo.status,
                "invited_by": uo.invited_by,
                "invited_at": uo.invited_at,
                "accepted_at": uo.accepted_at,
            })
        return out


__all__ = ["UserOrganizationsRepository"]
