"""Shared user-management helpers for /org/api/users/* and /org/api/orgs/{id}/users/invite.

Phase 1.4 — `invite_member()` is the single source of truth for issuing
org invites; both the legacy `org_api_invite_user` (path-id style) and the
new `/org/api/users/invite` (session-scoped) call it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import BackgroundTasks, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.models.branches import Branch
from src.models.org_invites import OrgInvite
from src.models.regions import Region
from src.models.users import User
from src.repositories.org_invites_repo import OrgInvitesRepository
from src.repositories.organizations_repo import OrganizationsRepository
from src.repositories.users_repo import UsersRepository
from src.services.email_service import send_org_invite_email
from src.services.org_audit_service import log_org_action

VALID_ROLES: set[str] = {
    "org_admin", "region_manager", "branch_manager", "coach", "viewer",
}


async def _validate_scope(
    db: AsyncSession,
    *,
    org_id: int,
    role: str,
    region_id: int | None,
    branch_id: int | None,
) -> None:
    """Enforce the role/scope rules at the application layer.

    - region_manager: region_id REQUIRED, branch_id MUST be NULL.
    - branch_manager: branch_id REQUIRED.
    - region_id (if given) must belong to org_id.
    - branch_id (if given) must belong to org_id (and its region too if set).

    Raises NotFoundError (404) when a referenced region/branch isn't in the
    org — preserves the standard 404-not-403 rule for cross-org access."""
    if role == "region_manager":
        if region_id is None:
            raise ValidationError(
                "region_manager requires region_id.", code="region_required"
            )
        if branch_id is not None:
            raise ValidationError(
                "region_manager must not have a branch scope.", code="invalid_scope"
            )
    if role == "branch_manager" and branch_id is None:
        raise ValidationError(
            "branch_manager requires branch_id.", code="branch_required"
        )

    if region_id is not None:
        region = (
            await db.execute(
                select(Region).where(
                    Region.id == region_id, Region.organization_id == org_id
                )
            )
        ).scalar_one_or_none()
        if region is None:
            raise NotFoundError("Region not found")

    if branch_id is not None:
        branch = (
            await db.execute(
                select(Branch).where(
                    Branch.id == branch_id, Branch.organization_id == org_id
                )
            )
        ).scalar_one_or_none()
        if branch is None:
            raise NotFoundError("Branch not found")
        # If both region_id and branch_id are set, branch must live in that region.
        if region_id is not None and branch.region_id != region_id:
            raise ValidationError(
                "Branch is not in the given region.", code="branch_region_mismatch"
            )


async def invite_member(
    db: AsyncSession,
    *,
    request: Request,
    background: BackgroundTasks,
    org_id: int,
    inviter: User,
    email: str,
    role: str,
    region_id: int | None = None,
    branch_id: int | None = None,
) -> OrgInvite:
    """Issue an org_invite token + create the OrgInvite row + log audit.

    Returns the persisted OrgInvite. The raw token NEVER leaves this function —
    only the email recipient sees it (via the email body). 409 on duplicate
    pending invite (org, email, role). 404 if the org is missing/deleted.
    """
    role = role.strip().lower()
    if role not in VALID_ROLES:
        raise ValidationError("Unknown role.", code="invalid_role")

    await _validate_scope(
        db, org_id=org_id, role=role, region_id=region_id, branch_id=branch_id,
    )

    org = await OrganizationsRepository(db).get(org_id)
    if not org or org.deleted_at is not None:
        raise NotFoundError("Organization not found")

    invitee_email = email.strip().lower()
    invites_repo = OrgInvitesRepository(db)
    if await invites_repo.get_pending(
        organization_id=org_id, email=invitee_email, role=role,
    ):
        raise ConflictError(
            "A pending invite already exists for this email + role.",
            code="invite_pending",
        )

    existing_user = await UsersRepository(db).get_by_email_active(invitee_email)
    _raw_token, auth_token_id = await send_org_invite_email(
        db,
        inviter_display_name=inviter.display_name or inviter.email,
        invitee_email=invitee_email,
        invitee_user_id=existing_user.id if existing_user else None,
        organization_name=org.name,
        role=role,
        background=background,
    )

    invite = OrgInvite(
        organization_id=org_id,
        email=invitee_email,
        role=role,
        region_id=region_id,
        branch_id=branch_id,
        auth_token_id=auth_token_id,
        invited_by=inviter.id,
        status="pending",
    )
    await invites_repo.create(invite)

    await log_org_action(
        db,
        organization_id=org_id,
        actor_user_id=inviter.id,
        actor_email=inviter.email,
        action="user.invite",
        target_type="org_invite",
        target_id=invite.id,
        request=request,
        extra={"email": invitee_email, "role": role},
    )

    # Make sure created_at is populated for the response (server_default may
    # be a no-op pre-commit in some SQLite paths).
    if invite.created_at is None:
        invite.created_at = datetime.now(UTC).replace(tzinfo=None)
    return invite


__all__ = ["VALID_ROLES", "invite_member"]
