"""FastAPI dependencies for `/org/*` (Org Admin) routes.

Three independent auth contexts coexist on a single request:
- Coach JWT (`get_current_user`) — cookie or Bearer
- System Admin session (`get_current_admin`) — `request.session["admin_email"]`
- **Org Admin session (this module)** — `request.session["org_user_id"]`

The 404-not-403 rule applies throughout: any cross-org access, missing role,
or session-tamper is rejected with `NotFoundError` (404), never
`ForbiddenError` (403). `UnauthorizedError` (401) is reserved for the
"no session at all" case (i.e., login required).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.exceptions import NotFoundError, UnauthorizedError
from src.middleware.org_context import (
    ORG_ACTIVE_ORG_KEY,
    ORG_ACTIVE_ROLE_KEY,
    ORG_SESSION_KEY,
)
from src.models.user_organizations import UserOrganization
from src.models.users import User
from src.repositories.user_organizations_repo import UserOrganizationsRepository
from src.repositories.users_repo import UsersRepository

__all__ = [
    "ORG_ACTIVE_ORG_KEY",
    "ORG_ACTIVE_ROLE_KEY",
    "ORG_SESSION_KEY",
    "get_current_org_user",
    "get_current_org_membership",
    "require_role",
]


async def get_current_org_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the logged-in Org Admin user from the session cookie.

    Raises `UnauthorizedError` (401) when there's no session at all — the
    caller (route or HTML page) decides whether to redirect to /org/login
    or return JSON. Stamps `request.state.user` so `page_context` can
    render org-aware templates."""
    user_id = request.session.get(ORG_SESSION_KEY)
    if not isinstance(user_id, int):
        raise UnauthorizedError("Org authentication required")

    user = await UsersRepository(db).get_active(user_id)
    if not user or not user.is_active:
        # Stale session — pop it so the next request re-prompts login.
        request.session.pop(ORG_SESSION_KEY, None)
        request.session.pop(ORG_ACTIVE_ORG_KEY, None)
        request.session.pop(ORG_ACTIVE_ROLE_KEY, None)
        raise UnauthorizedError("Org authentication required")

    request.state.user = user
    return user


async def get_current_org_membership(
    request: Request,
    user: User = Depends(get_current_org_user),
    db: AsyncSession = Depends(get_db),
) -> UserOrganization:
    """Resolve the active membership row (org + role + scope).

    404 (NOT 403) on any mismatch — covers session tamper, removed
    membership, or suspended status. The caller never gets to confirm the
    org exists. Stamps `request.state.org_id`, `org_role`, `org_membership`
    for downstream code (RLS via `get_db`, audit logging)."""
    org_id = request.session.get(ORG_ACTIVE_ORG_KEY)
    role = request.session.get(ORG_ACTIVE_ROLE_KEY)
    if not isinstance(org_id, int) or not isinstance(role, str):
        raise NotFoundError("Organization not found")

    repo = UserOrganizationsRepository(db)
    membership = await repo.get_active(
        user_id=user.id, organization_id=org_id, role=role,
    )
    if not membership:
        raise NotFoundError("Organization not found")

    request.state.org_id = org_id
    request.state.org_role = role
    request.state.org_membership = membership
    return membership


def require_role(*allowed: str) -> Callable:
    """Dependency factory enforcing role-based access on `/org/*` routes.

    Membership.role NOT in `allowed` raises `NotFoundError` (404, NOT 403).
    Same dependency-graph pattern as `require_pro` / `require_active_subscription`.

    Usage:
        @router.post("/org/api/orgs/{org_id}/users/invite")
        async def invite_user(
            org_id: int,
            membership = Depends(require_role("org_admin", "region_manager")),
            db: AsyncSession = Depends(get_db),
        ):
            ..."""

    async def _checker(
        membership: UserOrganization = Depends(get_current_org_membership),
    ) -> UserOrganization:
        if membership.role not in allowed:
            raise NotFoundError("Organization not found")
        return membership

    return _checker
