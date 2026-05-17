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
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.repositories.user_organizations_repo import UserOrganizationsRepository
from src.repositories.users_repo import UsersRepository
from src.services.email_service import send_org_invite_email
from src.services.org_audit_service import log_org_action
from src.services.org_user_service import (
    VALID_ROLES,
    clamp_and_validate_inviter_authority,
    invite_member,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/users", tags=["org-users"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _validate_invite_team_id(
    db: AsyncSession,
    *,
    org_id: int,
    team_id: int,
    program_id: int | None,
    region_id: int | None,
) -> int:
    """Phase 14 — verify a team_id stamped on a coach invite is in the
    inviter's scope and not already assigned to a different coach.

    Scope rule: the team's `program_id` and `region_id` must match the
    invite's clamped scope when those are set. (For org_admin both may be
    None — any team in the org passes.) Cross-scope → 404 cloak.

    Pre-redeem conflict check: if the team already has `user_id` set to
    someone else, raise 409 with a clear message so the inviter detaches
    that coach via the team page before re-inviting. This avoids silent
    coach-replacement at redeem time.
    """
    team = (await db.execute(
        select(TeamProfile).where(
            TeamProfile.id == team_id,
            TeamProfile.organization_id == org_id,
        )
    )).scalar_one_or_none()
    if team is None:
        raise NotFoundError("Team not found")
    if program_id is not None and team.program_id != program_id:
        raise NotFoundError("Team not found")
    if region_id is not None and team.region_id != region_id:
        raise NotFoundError("Team not found")
    if team.user_id is not None:
        raise ConflictError(
            "This team already has an assigned coach. Detach the existing "
            "coach from the team page before inviting a replacement.",
            code="team_already_has_coach",
        )
    return team.id


def _serialize_member(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "membership_id": row["membership_id"],
        "user_id": row["user_id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "program_id": row.get("program_id"),
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
    """Return scope filters the current actor is constrained to. Empty dict
    means org-wide visibility (org_admin).

    program_manager and region_manager each see only their own subtree —
    matches the invitation tree in `ROLE_INVITES_TREE` so listed members
    are exactly the population they can act on."""
    role = membership.role
    if role == "program_manager":
        return {"program_id": getattr(membership, "program_id", None)}
    if role == "region_manager":
        return {"region_id": membership.region_id}
    if role == "branch_manager":
        return {"branch_id": membership.branch_id}
    return {}


def _ensure_invite_visible(
    invite: OrgInvite | None, membership: UserOrganization
) -> OrgInvite:
    """Scope guard for invite read/update paths. 404 covers cross-org AND
    out-of-subtree scope mismatches."""
    if invite is None or invite.organization_id != membership.organization_id:
        raise NotFoundError("Invite not found")
    role = membership.role
    if role == "program_manager":
        if getattr(invite, "program_id", None) != getattr(membership, "program_id", None):
            raise NotFoundError("Invite not found")
    elif role == "region_manager":
        if invite.region_id != membership.region_id:
            raise NotFoundError("Invite not found")
    elif role == "branch_manager":
        if invite.branch_id != membership.branch_id:
            raise NotFoundError("Invite not found")
    return invite


# ---------------------------------------------------------------------------
# GET /org/api/users — list members
# ---------------------------------------------------------------------------


@router.get("", response_model=dict)
async def list_users(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role(
            "org_admin", "program_manager", "region_manager", "branch_manager",
        )
    ),
    region_id: int | None = Query(default=None),
    program_id: int | None = Query(default=None),
) -> dict:
    """List members of the active org. Every non-admin role is auto-scoped
    to its own subtree (program/region/branch); org_admin may narrow further
    via the query string."""
    repo = UserOrganizationsRepository(db)
    scope_kwargs = _scope_for_role(membership)
    # org_admin caller asked for a specific scope — apply on top of base.
    if membership.role == "org_admin":
        if region_id is not None:
            scope_kwargs = {**scope_kwargs, "region_id": region_id}
        if program_id is not None:
            scope_kwargs = {**scope_kwargs, "program_id": program_id}
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
        require_role(
            "org_admin", "program_manager", "region_manager", "branch_manager",
        )
    ),
) -> dict:
    """Issue an invite token + email it. Body shape mirrors `OrgInviteRequest`
    but we keep it as `dict` so we can apply role-aware validation BEFORE the
    Pydantic schema rejects.

    Authorization (per `ROLE_INVITES_TREE` in org_user_service):
      org_admin       -> any role
      program_manager -> region_manager / coach / viewer in inviter's program
      region_manager  -> coach / viewer / branch_manager in inviter's region
      branch_manager  -> coach in inviter's branch (legacy)
      coach / viewer  -> never reach this gate (require_role rejects them)

    `clamp_and_validate_inviter_authority` auto-fills program/region/branch
    from the inviter's own scope when not provided, and 404s on any
    cross-scope attempt. Same 404-not-403 rule that protects org-level reads.
    """
    email = (body.get("email") or "").strip().lower()
    role = (body.get("role") or "").strip().lower()
    program_id = body.get("program_id")
    region_id = body.get("region_id")
    branch_id = body.get("branch_id")
    team_id = body.get("team_id")

    if not email or "@" not in email:
        raise ValidationError("A valid email is required.", code="invalid_email")
    if role not in VALID_ROLES:
        raise ValidationError("Unknown role.", code="invalid_role")

    # Phase 14 — team_id is only meaningful for coach invites. Reject loud
    # if the caller stamped a team on a non-coach invite (PM/RM/admin/viewer
    # don't "own" a team in the TeamProfile.user_id sense).
    if team_id is not None and role != "coach":
        raise ValidationError(
            "team_id is only allowed when inviting a coach.",
            code="team_id_only_for_coach",
        )

    program_id, region_id, branch_id = await clamp_and_validate_inviter_authority(
        db,
        inviter=membership,
        target_role=role,
        program_id=program_id,
        region_id=region_id,
        branch_id=branch_id,
    )

    # Phase 14 — validate team_id is in the inviter's scope. Reject 404 on
    # cross-scope. Also check the team doesn't already have a different
    # coach assigned — the inviter has to clean that up via the team page
    # first (avoids silent coach-replacement at redeem time).
    if team_id is not None:
        team_id = await _validate_invite_team_id(
            db,
            org_id=membership.organization_id,
            team_id=int(team_id),
            program_id=program_id,
            region_id=region_id,
        )

    invite = await invite_member(
        db,
        request=request,
        background=background,
        org_id=membership.organization_id,
        inviter=request.state.user,
        email=email,
        role=role,
        program_id=program_id,
        region_id=region_id,
        branch_id=branch_id,
        team_id=team_id,
    )

    return {
        "id": invite.id,
        "organization_id": invite.organization_id,
        "email": invite.email,
        "role": invite.role,
        "status": invite.status,
        "program_id": invite.program_id,
        "region_id": invite.region_id,
        "branch_id": invite.branch_id,
        "team_id": invite.team_id,
        "short_code": invite.short_code,
        "created_at": invite.created_at.isoformat() if invite.created_at else None,
    }


# ---------------------------------------------------------------------------
# GET /org/api/users/invites/pending — list pending invites
# ---------------------------------------------------------------------------


@router.get("/invites/pending", response_model=dict)
async def list_pending_invites(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role(
            "org_admin", "program_manager", "region_manager", "branch_manager",
        )
    ),
) -> dict:
    stmt = select(OrgInvite).where(
        OrgInvite.organization_id == membership.organization_id,
        OrgInvite.status == "pending",
    )
    if membership.role == "program_manager":
        stmt = stmt.where(OrgInvite.program_id == getattr(membership, "program_id", None))
    elif membership.role == "region_manager":
        stmt = stmt.where(OrgInvite.region_id == membership.region_id)
    elif membership.role == "branch_manager":
        stmt = stmt.where(OrgInvite.branch_id == membership.branch_id)

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
                "program_id": getattr(r, "program_id", None),
                "region_id": r.region_id,
                "branch_id": r.branch_id,
                "short_code": getattr(r, "short_code", None),
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
        require_role(
            "org_admin", "program_manager", "region_manager", "branch_manager",
        )
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
        require_role(
            "org_admin", "program_manager", "region_manager", "branch_manager",
        )
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
    new_program_id = body.get("program_id", getattr(target, "program_id", None))
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
        program_id=new_program_id,
        region_id=new_region_id,
        branch_id=new_branch_id,
    )

    changes: dict = {}
    if new_role is not None and new_role != target.role:
        changes["role"] = {"from": target.role, "to": new_role}
        target.role = new_role
    if "program_id" in body and new_program_id != getattr(target, "program_id", None):
        changes["program_id"] = {
            "from": getattr(target, "program_id", None), "to": new_program_id,
        }
        target.program_id = new_program_id
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
