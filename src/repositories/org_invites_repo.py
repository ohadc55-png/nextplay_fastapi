"""OrgInvites repository — pending invite metadata.

The single-use token itself lives in `auth_tokens` (purpose='org_invite');
this row stores the org/role/scope the token unlocks.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.org_invites import OrgInvite
from src.repositories.org_scoped_repository import OrgScopedRepository


class OrgInvitesRepository(OrgScopedRepository[OrgInvite]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, OrgInvite)

    async def get_pending(
        self, *, organization_id: int, email: str, role: str
    ) -> OrgInvite | None:
        """Find an existing pending invite for the same (org, email, role).
        Used to prevent duplicate invites from the same flow."""
        stmt = select(OrgInvite).where(
            OrgInvite.organization_id == organization_id,
            OrgInvite.email == email.lower(),
            OrgInvite.role == role,
            OrgInvite.status == "pending",
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_auth_token_id(self, auth_token_id: int) -> OrgInvite | None:
        """Lookup invite by the auth_tokens.id it references. Used during
        invite acceptance after token validation."""
        stmt = select(OrgInvite).where(OrgInvite.auth_token_id == auth_token_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["OrgInvitesRepository"]
