"""Shared user-management helpers for /org/api/users/* and /org/api/orgs/{id}/users/invite.

Phase 1.4 — `invite_member()` is the single source of truth for issuing
org invites; both the legacy `org_api_invite_user` (path-id style) and the
new `/org/api/users/invite` (session-scoped) call it.

Phase 5+ — Adds a short human-readable redemption code on top of the magic
link. Both paths share the same auth_token, so redemption is one-time-use
across both surfaces — the moment the code is used, the link dies too.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import BackgroundTasks, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.models.branches import Branch
from src.models.org_invites import OrgInvite
from src.models.programs import Program
from src.models.regions import Region
from src.models.users import User
from src.repositories.org_invites_repo import OrgInvitesRepository
from src.repositories.organizations_repo import OrganizationsRepository
from src.repositories.users_repo import UsersRepository
from src.services.email_service import send_org_invite_email
from src.services.org_audit_service import log_org_action

# Unambiguous 31-char alphabet — letters that look alike (0/O, 1/I/L) are
# dropped so a code typed from a paper note or WhatsApp screenshot can't be
# mis-read. 31^8 ≈ 8.5 × 10^11 — collision space is comfortably large.
_SHORT_CODE_ALPHABET: str = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_SHORT_CODE_LENGTH: int = 8


def generate_short_code() -> str:
    """Return a fresh 8-char invite code. Caller must check uniqueness."""
    return "".join(secrets.choice(_SHORT_CODE_ALPHABET) for _ in range(_SHORT_CODE_LENGTH))


async def allocate_unique_short_code(db: AsyncSession, *, max_tries: int = 8) -> str:
    """Generate a code that doesn't collide with an existing invite.

    Retries up to 8 times — at 31^8 ≈ 8.5×10^11 the birthday-bound collision
    probability across 10^5 active invites is < 10^-7, so retries rarely fire.
    The hard 8-retry cap stops a misconfigured DB from looping forever.
    """
    for _ in range(max_tries):
        code = generate_short_code()
        hit = (await db.execute(
            select(OrgInvite.id).where(OrgInvite.short_code == code).limit(1)
        )).scalar_one_or_none()
        if hit is None:
            return code
    # Fallback — append entropy. Extremely unlikely to be hit.
    return generate_short_code() + secrets.token_hex(2).upper()

VALID_ROLES: set[str] = {
    "org_admin", "program_manager", "region_manager", "coach", "viewer",
}


# Sha'ar Shivyon org hierarchy (Phase 12 — branch_manager was retired):
#   org_admin → program_manager → region_manager → coach
# Plus an org-wide `viewer` (read-only auditor) that any tier above can grant.
# Pure data — tests + the user-service consume this directly.
ROLE_INVITES_TREE: dict[str, frozenset[str]] = {
    "org_admin":       frozenset({
        "org_admin", "program_manager", "region_manager", "coach", "viewer",
    }),
    "program_manager": frozenset({"region_manager", "coach", "viewer"}),
    "region_manager":  frozenset({"coach", "viewer"}),
    "coach":           frozenset(),
    "viewer":          frozenset(),
}


async def clamp_and_validate_inviter_authority(
    db: AsyncSession,
    *,
    inviter: object,  # UserOrganization — kept as object to avoid the typing import cycle
    target_role: str,
    program_id: int | None,
    region_id: int | None,
    branch_id: int | None,
) -> tuple[int | None, int | None, int | None]:
    """Enforce the invitation tree + clamp target scope to inviter's subtree.

    Returns the (possibly auto-filled) `(program_id, region_id, branch_id)`
    tuple. Auto-fill happens when the inviter is scope-constrained and the
    body didn't pass a value — saves the front-end from echoing back a value
    the server already knows.

    Raises `NotFoundError` (404) on every authority failure — never 403, so
    cross-scope probes can't enumerate orgs/programs/regions. Conditions:
    - inviter's role can't invite `target_role` (per `ROLE_INVITES_TREE`)
    - target's `program_id` falls outside inviter's program (program_manager)
    - target's `region_id`  falls outside inviter's region  (region_manager)
    - target's `branch_id`  falls outside inviter's branch  (branch_manager)
    - when a `region_id` is passed by a program_manager, that region must
      belong to the inviter's program (region.program_id == inviter.program_id)
    """
    inviter_role: str = getattr(inviter, "role", "")
    inviter_org_id: int = getattr(inviter, "organization_id", 0)
    allowed = ROLE_INVITES_TREE.get(inviter_role, frozenset())
    if target_role not in allowed:
        raise NotFoundError("Organization not found")

    if inviter_role == "program_manager":
        inviter_program_id = getattr(inviter, "program_id", None)
        if inviter_program_id is None:
            # Misconfigured PM membership — defensive 404 rather than crash.
            raise NotFoundError("Program not found")
        # Phase 12 — Sha'ar Shivyon org model:
        #   program_manager → must be in PM's program
        #   region_manager  → must be in PM's program (RM is sub-tier of PM)
        #   coach           → in PM's program
        #   viewer          → read-only, not program-bound
        # A PM that tries to invite into a different program is rejected
        # (404 cloak — never confirms cross-program existence).
        carries_program = target_role in ("program_manager", "region_manager", "coach")
        if carries_program:
            if program_id is None:
                program_id = inviter_program_id
            elif program_id != inviter_program_id:
                raise NotFoundError("Program not found")
        else:
            if program_id is not None and program_id != inviter_program_id:
                raise NotFoundError("Program not found")
            program_id = None
        if region_id is not None:
            # Phase 12 — regions are program-agnostic by default (NULL
            # program_id = shared geography, any PM in the org may use it).
            # Regions that ARE pinned to a specific program belong to that
            # program only — a PM can't invite into a region owned by
            # another program. The 404 cloak applies to both failure modes.
            region = (
                await db.execute(
                    select(Region).where(
                        Region.id == region_id,
                        Region.organization_id == inviter_org_id,
                    )
                )
            ).scalar_one_or_none()
            if region is None:
                raise NotFoundError("Region not found")
            if region.program_id is not None and region.program_id != inviter_program_id:
                raise NotFoundError("Region not found")

    elif inviter_role == "region_manager":
        inviter_region_id = getattr(inviter, "region_id", None)
        if inviter_region_id is None:
            raise NotFoundError("Region not found")
        if region_id is None:
            region_id = inviter_region_id
        elif region_id != inviter_region_id:
            raise NotFoundError("Region not found")

    return program_id, region_id, branch_id


async def _validate_scope(
    db: AsyncSession,
    *,
    org_id: int,
    role: str,
    program_id: int | None,
    region_id: int | None,
    branch_id: int | None,
) -> None:
    """Enforce the role/scope rules at the application layer (Phase 12).

    Sha'ar Shivyon hierarchy:
      - org_admin:       no scope required.
      - program_manager: program_id REQUIRED, region_id MUST be NULL.
      - region_manager:  region_id AND program_id BOTH REQUIRED (an RM is
                         always "RM of {region} – {program}").
      - coach:           no scope required (team ownership handles it).
      - viewer:          no scope required.

    Raises NotFoundError (404) when a referenced program/region isn't in the
    org — preserves the standard 404-not-403 rule for cross-org access."""
    if role == "program_manager":
        if program_id is None:
            raise ValidationError(
                "program_manager requires program_id.", code="program_required"
            )
        if region_id is not None:
            raise ValidationError(
                "program_manager must not have a region scope.",
                code="invalid_scope",
            )
    if role == "region_manager":
        if region_id is None:
            raise ValidationError(
                "region_manager requires region_id.", code="region_required"
            )
        if program_id is None:
            raise ValidationError(
                "region_manager requires program_id.", code="program_required"
            )

    if program_id is not None:
        program = (
            await db.execute(
                select(Program).where(
                    Program.id == program_id, Program.organization_id == org_id
                )
            )
        ).scalar_one_or_none()
        if program is None:
            raise NotFoundError("Program not found")

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


# ---------------------------------------------------------------------------
# Enterprise Club helper (Phase 14)
# ---------------------------------------------------------------------------


async def get_or_create_enterprise_club(
    db: AsyncSession, organization
) -> Any:
    """Return the `Club` row that backs this enterprise Organization.

    Coaches invited through an org need `users.club_id` populated so the
    trial/purge state machine (`@check_subscription`) treats them as club
    members and skips the 30-day trial. Rather than maintaining a separate
    `organizations.club_id` FK + migration, we lazily get-or-create a Club
    row whose name matches the org. Pure-additive; same org always resolves
    to the same Club id.
    """
    from sqlalchemy import select as _select

    from src.models.clubs import Club

    existing = (await db.execute(
        _select(Club).where(Club.name == organization.name)
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    club = Club(
        name=organization.name,
        # Plan is informational only on the Club row — the user-facing
        # `subscription_plan` is set on the User directly (to "club"). The
        # default "academy10" is fine but we set "enterprise" for clarity.
        subscription_plan="enterprise",
        max_seats=9999,
        pooled_storage_gb=9999,
    )
    db.add(club)
    await db.flush()
    return club


async def invite_member(
    db: AsyncSession,
    *,
    request: Request,
    background: BackgroundTasks,
    org_id: int,
    inviter: User,
    email: str,
    role: str,
    program_id: int | None = None,
    region_id: int | None = None,
    branch_id: int | None = None,
    team_id: int | None = None,
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
        db,
        org_id=org_id,
        role=role,
        program_id=program_id,
        region_id=region_id,
        branch_id=branch_id,
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
    short_code = await allocate_unique_short_code(db)
    _raw_token, auth_token_id = await send_org_invite_email(
        db,
        inviter_display_name=inviter.display_name or inviter.email,
        invitee_email=invitee_email,
        invitee_user_id=existing_user.id if existing_user else None,
        organization_name=org.name,
        role=role,
        background=background,
        short_code=short_code,
    )

    invite = OrgInvite(
        organization_id=org_id,
        email=invitee_email,
        role=role,
        program_id=program_id,
        region_id=region_id,
        branch_id=branch_id,
        team_id=team_id,
        auth_token_id=auth_token_id,
        invited_by=inviter.id,
        status="pending",
        short_code=short_code,
    )
    await invites_repo.create(invite)

    audit_extra: dict[str, Any] = {"email": invitee_email, "role": role}
    if team_id is not None:
        audit_extra["team_id"] = team_id
    await log_org_action(
        db,
        organization_id=org_id,
        actor_user_id=inviter.id,
        actor_email=inviter.email,
        action="user.invite",
        target_type="org_invite",
        target_id=invite.id,
        request=request,
        extra=audit_extra,
    )

    # Make sure created_at is populated for the response (server_default may
    # be a no-op pre-commit in some SQLite paths).
    if invite.created_at is None:
        invite.created_at = datetime.now(UTC).replace(tzinfo=None)
    return invite


__all__ = [
    "ROLE_INVITES_TREE",
    "get_or_create_enterprise_club",
    "VALID_ROLES",
    "allocate_unique_short_code",
    "clamp_and_validate_inviter_authority",
    "generate_short_code",
    "invite_member",
]
