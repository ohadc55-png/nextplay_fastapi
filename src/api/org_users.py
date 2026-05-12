"""User Management under /org/api/users/* (Phase 1.4).

Endpoints (auth: org session; role-gated as marked):

- GET    /org/api/users                          list members
                                                 (org_admin / region_manager-of-region)
- POST   /org/api/users/invite                   issue new invite
                                                 (org_admin / region_manager-of-region)
- GET    /org/api/users/invites/pending          list still-pending invites
                                                 (org_admin / region_manager-of-region)
- POST   /org/api/users/invites/{id}/resend      re-mint token + email; invalidate old
                                                 (org_admin / region_manager-of-region)
- DELETE /org/api/users/invites/{id}             cancel pending invite
                                                 (org_admin / region_manager-of-region)
- PATCH  /org/api/users/{membership_id}          change role + scope
                                                 (org_admin only)
- DELETE /org/api/users/{membership_id}          soft-remove (status='removed')
                                                 (org_admin only)

404-not-403 throughout: every cross-org / cross-region access returns 404.
Region managers are *capped* — they cannot invite/promote to org_admin or
region_manager (would be self-escalation).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import require_role
from src.core.database import get_db
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.models.auth import AuthToken
from src.models.org_invites import OrgInvite
from src.models.user_organizations import UserOrganization
from src.repositories.user_organizations_repo import UserOrganizationsRepository
from src.repositories.users_repo import UsersRepository
from src.services.email_service import send_org_invite_email
from src.services.org_audit_service import log_org_action
from src.services.org_user_service import VALID_ROLES, invite_member

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/users", tags=["org-users"])

# Roles a region_manager is allowed to issue invites for. Excludes
# org_admin + region_manager (self-escalation guard).
_RM_INVITABLE_ROLES: set[str] = {"branch_manager", "coach", "viewer"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_member(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "membership_id": row["membership_id"],
        "user_id": row["user_id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "region_id": row["region_id"],
        "branch_id": row["branch_id"],
        "status": row["status"],
        "invited_by": row["invited_by"],
        "invited_at": row["invited_at"].isoformat() if row["invited_at"] else None,
        "accepted_at": row["accepted_at"].isoformat() if row["accepted_at"] else None,
    }


def _scope_for_role(
    membership: UserOrganization,
) -> dict[str, Any]:
    """Return scope filters (region_id) the current actor is constrained to.
    Empty dict means org-wide visibility (org_admin)."""
    if membership.role == "region_manager":
        return {"region_id": membership.region_id}
    return {}


def _ensure_invite_visible(
    invite: OrgInvite | None, membership: UserOrganization
) -> OrgInvite:
    """Scope guard for invite read/update paths. 404 covers cross-org AND
    out-of-region scope mismatches."""
    if invite is None or invite.organization_id != membership.organization_id:
        raise NotFoundError("Invite not found")
    if membership.role == "region_manager":
        if invite.region_id != membership.region_id:
            raise NotFoundError("Invite not found")
    return invite


# ---------------------------------------------------------------------------
# GET /org/api/users — list members
# ---------------------------------------------------------------------------


@router.get("", response_model=dict)
async def list_users(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager")
    ),
    region_id: int | None = Query(default=None),
) -> dict:
    """List members of the active org. region_manager's scope is enforced
    server-side (forced to own region); org_admin may pass `?region_id=` to
    narrow the list."""
    repo = UserOrganizationsRepository(db)
    scope_kwargs = _scope_for_role(membership)
    # org_admin caller asked for a specific region — apply on top of base scope.
    if region_id is not None and membership.role == "org_admin":
        scope_kwargs = {**scope_kwargs, "region_id": region_id}
    rows = await repo.list_with_user_data(
        membership.organization_id, **scope_kwargs,
    )
    rows.sort(key=lambda r: (r["role"], r["email"]))
    return {"members": [_serialize_member(r) for r in rows]}


# ---------------------------------------------------------------------------
# POST /org/api/users/invite — issue new invite
# ---------------------------------------------------------------------------


@router.post("/invite", response_model=dict, status_code=status.HTTP_201_CREATED)
async def invite_user(
    body: dict,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager")
    ),
) -> dict:
    """Issue an invite token + email it. Body shape mirrors `OrgInviteRequest`
    but we keep it as `dict` so we can apply role-aware validation BEFORE the
    Pydantic schema rejects.

    region_manager constraints (enforced server-side, regardless of body):
    - target role MUST be in `_RM_INVITABLE_ROLES`
    - region_id (if set) MUST equal membership.region_id
    - if branch_id is given, the branch must live in their region (validated
      via `_validate_scope` in invite_member).
    """
    email = (body.get("email") or "").strip().lower()
    role = (body.get("role") or "").strip().lower()
    region_id = body.get("region_id")
    branch_id = body.get("branch_id")

    if not email or "@" not in email:
        raise ValidationError("A valid email is required.", code="invalid_email")
    if role not in VALID_ROLES:
        raise ValidationError("Unknown role.", code="invalid_role")

    # Region-manager scope clamp: can't escalate above themselves, can't
    # invite outside their region.
    if membership.role == "region_manager":
        if role not in _RM_INVITABLE_ROLES:
            raise NotFoundError("Organization not found")
        if region_id is None:
            region_id = membership.region_id
        elif region_id != membership.region_id:
            raise NotFoundError("Region not found")

    invite = await invite_member(
        db,
        request=request,
        background=background,
        org_id=membership.organization_id,
        inviter=request.state.user,
        email=email,
        role=role,
        region_id=region_id,
        branch_id=branch_id,
    )

    return {
        "id": invite.id,
        "organization_id": invite.organization_id,
        "email": invite.email,
        "role": invite.role,
        "status": invite.status,
        "region_id": invite.region_id,
        "branch_id": invite.branch_id,
        "created_at": invite.created_at.isoformat() if invite.created_at else None,
    }


# ---------------------------------------------------------------------------
# GET /org/api/users/invites/pending — list pending invites
# ---------------------------------------------------------------------------


@router.get("/invites/pending", response_model=dict)
async def list_pending_invites(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager")
    ),
) -> dict:
    stmt = select(OrgInvite).where(
        OrgInvite.organization_id == membership.organization_id,
        OrgInvite.status == "pending",
    )
    if membership.role == "region_manager":
        stmt = stmt.where(OrgInvite.region_id == membership.region_id)

    rows = list((await db.execute(stmt)).scalars().all())
    rows.sort(key=lambda r: (r.created_at or datetime.min), reverse=True)
    return {
        "invites": [
            {
                "id": r.id,
                "organization_id": r.organization_id,
                "email": r.email,
                "role": r.role,
                "status": r.status,
                "region_id": r.region_id,
                "branch_id": r.branch_id,
                "invited_by": r.invited_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# POST /org/api/users/invites/{invite_id}/resend
# ---------------------------------------------------------------------------


@router.post("/invites/{invite_id}/resend", response_model=dict)
async def resend_invite(
    invite_id: int,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager")
    ),
) -> dict:
    """Re-mint a fresh 7-day token + email, and mark the OLD token used so it
    can't double-sign-up. Idempotent in the sense that the invite row's email,
    role, scope all stay the same — only auth_token_id swaps.
    """
    invite = (
        await db.execute(
            select(OrgInvite).where(OrgInvite.id == invite_id)
        )
    ).scalar_one_or_none()
    _ensure_invite_visible(invite, membership)
    if invite.status != "pending":
        raise ConflictError(
            "Only pending invites can be resent.", code="not_pending"
        )

    # Mark the old auth_token used so the prior email link stops working.
    old_token = await db.get(AuthToken, invite.auth_token_id)
    if old_token is not None and old_token.used_at is None:
        old_token.used_at = datetime.utcnow()

    # Issue a fresh token via the same email helper used for new invites.
    invitee_user = await UsersRepository(db).get_by_email_active(invite.email)
    from src.repositories.organizations_repo import OrganizationsRepository
    org = await OrganizationsRepository(db).get(invite.organization_id)
    if not org:
        raise NotFoundError("Organization not found")

    _raw_token, new_auth_token_id = await send_org_invite_email(
        db,
        inviter_display_name=(
            request.state.user.display_name or request.state.user.email
        ),
        invitee_email=invite.email,
        invitee_user_id=invitee_user.id if invitee_user else None,
        organization_name=org.name,
        role=invite.role,
        background=background,
    )

    invite.auth_token_id = new_auth_token_id
    await db.flush()

    await log_org_action(
        db,
        organization_id=invite.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="user.invite.resend",
        target_type="org_invite",
        target_id=invite.id,
        request=request,
        extra={"email": invite.email, "role": invite.role},
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# DELETE /org/api/users/invites/{invite_id} — cancel pending invite
# ---------------------------------------------------------------------------


@router.delete("/invites/{invite_id}", response_model=dict)
async def cancel_invite(
    invite_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager")
    ),
) -> dict:
    invite = (
        await db.execute(select(OrgInvite).where(OrgInvite.id == invite_id))
    ).scalar_one_or_none()
    _ensure_invite_visible(invite, membership)
    if invite.status != "pending":
        raise ConflictError(
            "Only pending invites can be cancelled.", code="not_pending"
        )

    invite.status = "cancelled"
    token = await db.get(AuthToken, invite.auth_token_id)
    if token is not None and token.used_at is None:
        token.used_at = datetime.utcnow()
    await db.flush()

    await log_org_action(
        db,
        organization_id=invite.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="user.invite.cancel",
        target_type="org_invite",
        target_id=invite.id,
        request=request,
        extra={"email": invite.email, "role": invite.role},
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# PATCH /org/api/users/{membership_id} — change role + scope (org_admin only)
# ---------------------------------------------------------------------------


@router.patch("/{membership_id}", response_model=dict)
async def update_member(
    membership_id: int,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(require_role("org_admin")),
) -> dict:
    """org_admin updates a member's role and/or scope. Cannot demote oneself —
    that's a guard against accidental lock-out.

    Body fields (all optional):
    - role: one of VALID_ROLES
    - region_id: int | null
    - branch_id: int | null
    """
    target = await db.get(UserOrganization, membership_id)
    if target is None or target.organization_id != membership.organization_id:
        raise NotFoundError("Member not found")

    # Self-demotion guard.
    if target.user_id == request.state.user.id and target.role == "org_admin":
        new_role = body.get("role")
        if new_role and new_role != "org_admin":
            raise ConflictError(
                "Cannot demote yourself. Promote another org_admin first.",
                code="self_demotion_blocked",
            )

    new_role = body.get("role")
    new_region_id = body.get("region_id", target.region_id)
    new_branch_id = body.get("branch_id", target.branch_id)
    final_role = (new_role or target.role).strip().lower()

    if new_role is not None and final_role not in VALID_ROLES:
        raise ValidationError("Unknown role.", code="invalid_role")

    # Scope validation goes through the same helper as the invite path.
    from src.services.org_user_service import _validate_scope
    await _validate_scope(
        db,
        org_id=membership.organization_id,
        role=final_role,
        region_id=new_region_id,
        branch_id=new_branch_id,
    )

    changes: dict = {}
    if new_role is not None and new_role != target.role:
        changes["role"] = {"from": target.role, "to": new_role}
        target.role = new_role
    if "region_id" in body and new_region_id != target.region_id:
        changes["region_id"] = {"from": target.region_id, "to": new_region_id}
        target.region_id = new_region_id
    if "branch_id" in body and new_branch_id != target.branch_id:
        changes["branch_id"] = {"from": target.branch_id, "to": new_branch_id}
        target.branch_id = new_branch_id

    if changes:
        await db.flush()
        await db.refresh(target)
        action = (
            "member.role_change" if "role" in changes else "member.scope_change"
        )
        await log_org_action(
            db,
            organization_id=membership.organization_id,
            actor_user_id=request.state.user.id,
            actor_email=request.state.user.email,
            action=action,
            target_type="user_organization",
            target_id=target.id,
            request=request,
            extra=changes,
        )

    return {
        "membership_id": target.id,
        "user_id": target.user_id,
        "role": target.role,
        "region_id": target.region_id,
        "branch_id": target.branch_id,
        "status": target.status,
    }


# ---------------------------------------------------------------------------
# DELETE /org/api/users/{membership_id} — soft-remove (status='removed')
# ---------------------------------------------------------------------------


@router.delete("/{membership_id}", response_model=dict)
async def remove_member(
    membership_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(require_role("org_admin")),
) -> dict:
    target = await db.get(UserOrganization, membership_id)
    if target is None or target.organization_id != membership.organization_id:
        raise NotFoundError("Member not found")

    # Cannot remove yourself — same lock-out guard as PATCH.
    if target.user_id == request.state.user.id:
        raise ConflictError(
            "Cannot remove your own membership.", code="self_removal_blocked",
        )

    if target.status == "removed":
        return {"ok": True}

    snapshot = {
        "role": target.role,
        "region_id": target.region_id,
        "branch_id": target.branch_id,
    }
    target.status = "removed"
    await db.flush()

    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="member.remove",
        target_type="user_organization",
        target_id=target.id,
        request=request,
        extra=snapshot,
    )
    return {"ok": True}


__all__ = ["router"]
