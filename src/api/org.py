"""Org Admin (`/org/*`) JSON API + dual-shape login.

Mirrors the existing `/admin/*` admin-panel pattern:
- HTML pages live alongside `/org/login` (this file accepts both shapes)
  and in `src/api/org_pages.py` for GET-only renders.
- JSON-only endpoints live under `/org/api/*`.

Session keys: see `src.middleware.org_context`.
404-not-403 rule: any cross-org / role-mismatch / missing-membership returns
`NotFoundError` (404), never 403.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.api.deps.org_auth import (
    ORG_ACTIVE_ORG_KEY,
    ORG_ACTIVE_ROLE_KEY,
    ORG_SESSION_KEY,
    get_current_org_membership,
    get_current_org_user,
    require_role,
)
from src.auth.password_service import hash_password, verify_password
from src.core.database import get_db
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.frontend import page_context, templates
from src.models.auth import AuthToken
from src.models.branches import Branch
from src.models.org_invites import OrgInvite
from src.models.organizations import Organization
from src.models.regions import Region
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.models.users import User
from src.repositories.organizations_repo import OrganizationsRepository
from src.repositories.org_invites_repo import OrgInvitesRepository
from src.repositories.user_organizations_repo import UserOrganizationsRepository
from src.repositories.users_repo import UsersRepository
from src.schemas.org import (
    DashboardSummary,
    OrganizationOut,
    OrgInviteAcceptRequest,
    OrgInviteOut,
    OrgInviteRequest,
    OrgLoginRequest,  # noqa: F401 — referenced via OpenAPI docs
    OrgMeResponse,
    OrgRoleOption,
    OrgRoleSelection,
)
from src.services.email_service import send_org_invite_email
from src.services.org_audit_service import log_org_action

_VALID_ROLES = {"org_admin", "region_manager", "branch_manager", "coach", "viewer"}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org", tags=["org"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wants_html(request: Request) -> bool:
    """Mirrors src/api/admin.py:_wants_html — pick HTML vs JSON response shape."""
    ct = (request.headers.get("content-type") or "").lower()
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in ct:
        return False
    if "application/json" in accept and "text/html" not in accept:
        return False
    return True


def _render_login_with_error(request: Request, error: str, status_code: int = 404):
    """Re-render /org/login with an inline error. 404 (not 401) keeps the
    enumeration-resistance promise: invalid creds and no-such-account are
    indistinguishable."""
    ctx = page_context(request, user=None, extra={
        "error": error,
        "page_title": "Sign in",
    })
    return templates.TemplateResponse(
        "org/login.html", ctx, status_code=status_code,
    )


async def _hydrate_membership_options(
    db: AsyncSession, memberships: list[UserOrganization]
) -> list[OrgRoleOption]:
    """Build the role-selector list for multi-role users. Loads org names
    in one query so we don't trigger lazy='raise'."""
    if not memberships:
        return []
    org_ids = list({m.organization_id for m in memberships})
    orgs = list(
        (await db.execute(
            select(Organization).where(Organization.id.in_(org_ids))
        )).scalars().all()
    )
    by_id = {o.id: o for o in orgs}
    return [
        OrgRoleOption(
            organization_id=m.organization_id,
            organization_name=(by_id[m.organization_id].name if m.organization_id in by_id else "?"),
            organization_slug=(by_id[m.organization_id].slug if m.organization_id in by_id else "?"),
            role=m.role,
            region_id=m.region_id,
            branch_id=m.branch_id,
        )
        for m in memberships
    ]


def _set_active_membership(request: Request, membership: UserOrganization) -> None:
    """Promote a UserOrganization row to the session's "active" trio."""
    request.session[ORG_SESSION_KEY] = membership.user_id
    request.session[ORG_ACTIVE_ORG_KEY] = membership.organization_id
    request.session[ORG_ACTIVE_ROLE_KEY] = membership.role


# ---------------------------------------------------------------------------
# POST /org/login — dual-shape (HTML form OR JSON)
# ---------------------------------------------------------------------------


@router.post("/login", include_in_schema=True)
async def org_login(request: Request, db: AsyncSession = Depends(get_db)):
    """Authenticate an Org Admin user.

    On invalid credentials: 404 (NOT 401). On a valid login for a user with
    no org memberships: ALSO 404 — a private coach hitting `/org/login`
    sees the same error as a typo'd password, by design (no enumeration).
    """
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        try:
            body = await request.json()
        except Exception:
            body = {}
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
    else:
        form = await request.form()
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""

    html_response = _wants_html(request)

    user = await UsersRepository(db).get_by_email_active(email.lower())
    creds_ok = (
        user is not None
        and user.password_hash is not None
        and verify_password(password, user.password_hash)
    )
    if not creds_ok or not user:
        if html_response:
            return _render_login_with_error(request, "Invalid credentials")
        raise HTTPException(status_code=404, detail="Not found")

    memberships = await UserOrganizationsRepository(db).list_for_user(user.id)
    if not memberships:
        # The user exists but isn't an org member — same 404 to avoid
        # leaking that this email is registered.
        if html_response:
            return _render_login_with_error(request, "Invalid credentials")
        raise HTTPException(status_code=404, detail="Not found")

    if len(memberships) == 1:
        m = memberships[0]
        _set_active_membership(request, m)
        await log_org_action(
            db,
            organization_id=m.organization_id,
            actor_user_id=user.id,
            actor_email=user.email,
            action="auth.org.login",
            request=request,
        )
        if html_response:
            return RedirectResponse(url="/org/dashboard", status_code=303)
        return {"ok": True, "redirect": "/org/dashboard"}

    # Multi-role: pin the user (pending) and let them pick a role.
    request.session[ORG_SESSION_KEY] = user.id
    request.session.pop(ORG_ACTIVE_ORG_KEY, None)
    request.session.pop(ORG_ACTIVE_ROLE_KEY, None)
    options = await _hydrate_membership_options(db, memberships)

    if html_response:
        return RedirectResponse(url="/org/role-select", status_code=303)
    return {
        "status": "select_role",
        "roles": [o.model_dump() for o in options],
    }


# ---------------------------------------------------------------------------
# POST /org/logout — clears ORG keys only
# ---------------------------------------------------------------------------


@router.post("/logout")
async def org_logout(request: Request) -> dict:
    """Clear the Org Admin session keys. Does NOT touch admin_email (System
    Admin) or the JWT cookie (Coach). Idempotent."""
    request.session.pop(ORG_SESSION_KEY, None)
    request.session.pop(ORG_ACTIVE_ORG_KEY, None)
    request.session.pop(ORG_ACTIVE_ROLE_KEY, None)
    return {"ok": True, "redirect": "/org/login"}


# ---------------------------------------------------------------------------
# POST /org/switch-role — already logged in, picking a different role/org
# ---------------------------------------------------------------------------


@router.post("/switch-role")
async def org_switch_role(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_org_user),
):
    """Change the active (org_id, role) for the currently logged-in user.
    Dual-shape (HTML form OR JSON), mirroring `/org/login`. Used by both:
    (a) initial multi-role pick after login, and (b) mid-session role swap.
    404 if the requested membership doesn't exist or is suspended."""
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        body = dict(await request.form())

    try:
        selection = OrgRoleSelection.model_validate(
            {
                "organization_id": int(body.get("organization_id")),
                "role": str(body.get("role") or ""),
            }
        )
    except (TypeError, ValueError):
        raise NotFoundError("Organization not found") from None

    repo = UserOrganizationsRepository(db)
    membership = await repo.get_active(
        user_id=user.id,
        organization_id=selection.organization_id,
        role=selection.role,
    )
    if not membership:
        raise NotFoundError("Organization not found")

    _set_active_membership(request, membership)
    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=user.id,
        actor_email=user.email,
        action="auth.org.switch_role",
        target_type="user_organization",
        target_id=membership.id,
        request=request,
    )

    if _wants_html(request):
        return RedirectResponse(url="/org/dashboard", status_code=303)
    return {"ok": True, "redirect": "/org/dashboard"}


# ---------------------------------------------------------------------------
# GET /org/api/me — current user + active org/role
# ---------------------------------------------------------------------------


@router.get("/api/me", response_model=OrgMeResponse)
async def org_api_me(
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> OrgMeResponse:
    user = request.state.user  # stamped by get_current_org_user
    org = await OrganizationsRepository(db).get(membership.organization_id)
    if not org:
        # Membership references a deleted org — defensive 404.
        raise NotFoundError("Organization not found")
    return OrgMeResponse(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        organization_id=org.id,
        organization_name=org.name,
        organization_slug=org.slug,
        role=membership.role,
        region_id=membership.region_id,
        branch_id=membership.branch_id,
    )


# ---------------------------------------------------------------------------
# GET /org/api/roles — list memberships for the role-selector page
# ---------------------------------------------------------------------------


@router.get("/api/roles")
async def org_api_roles(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_org_user),
):
    """List active memberships for the logged-in user. The role-selector page
    fetches this; the multi-role login response also includes it."""
    memberships = await UserOrganizationsRepository(db).list_for_user(user.id)
    options = await _hydrate_membership_options(db, memberships)
    return {"roles": [o.model_dump() for o in options]}


# ---------------------------------------------------------------------------
# GET /org/api/dashboard/summary — header tiles
# ---------------------------------------------------------------------------


@router.get("/api/dashboard/summary", response_model=DashboardSummary)
async def org_api_dashboard_summary(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> DashboardSummary:
    org = await OrganizationsRepository(db).get(membership.organization_id)
    if not org:
        raise NotFoundError("Organization not found")

    # Count active members, branches, regions, teams scoped to this org.
    member_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(UserOrganization)
                .where(
                    UserOrganization.organization_id == org.id,
                    UserOrganization.status == "active",
                )
            )
        ).scalar()
        or 0
    )
    branch_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Branch)
                .where(Branch.organization_id == org.id)
            )
        ).scalar()
        or 0
    )
    region_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Region)
                .where(Region.organization_id == org.id)
            )
        ).scalar()
        or 0
    )
    team_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(TeamProfile)
                .where(TeamProfile.organization_id == org.id)
            )
        ).scalar()
        or 0
    )

    return DashboardSummary(
        organization=OrganizationOut.model_validate(org),
        role=membership.role,
        member_count=member_count,
        branch_count=branch_count,
        region_count=region_count,
        team_count=team_count,
    )


# ---------------------------------------------------------------------------
# Invite — issue + accept
# ---------------------------------------------------------------------------


@router.post("/api/orgs/{org_id}/users/invite", response_model=OrgInviteOut, status_code=201)
async def org_api_invite_user(
    org_id: int,
    body: OrgInviteRequest,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(require_role("org_admin")),
) -> OrgInviteOut:
    """Issue a single-use 7-day invite token + send the email (or log it in
    EMAIL_MODE=console). Returns the OrgInvite row metadata; the raw token
    is NEVER included in the response body — only the email recipient sees it.

    404 if the path's org_id doesn't match the inviter's active org (cross-org
    write attempt). Conflict if a pending invite already exists for the same
    (org, email, role) trio."""
    if membership.organization_id != org_id:
        raise NotFoundError("Organization not found")

    role = body.role.strip().lower()
    if role not in _VALID_ROLES:
        raise ValidationError("Unknown role", code="invalid_role")

    org = await OrganizationsRepository(db).get(org_id)
    if not org or org.deleted_at is not None:
        raise NotFoundError("Organization not found")

    invitee_email = body.email.lower()
    invites_repo = OrgInvitesRepository(db)
    if await invites_repo.get_pending(
        organization_id=org_id, email=invitee_email, role=role,
    ):
        raise ConflictError("A pending invite already exists for this email + role.")

    # Existing user, if any. Token can reference them; otherwise NULL.
    existing_user = await UsersRepository(db).get_by_email_active(invitee_email)
    inviter = request.state.user  # set by get_current_org_user

    raw_token, auth_token_id = await send_org_invite_email(
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
        region_id=body.region_id,
        branch_id=body.branch_id,
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

    return OrgInviteOut(
        id=invite.id,
        organization_id=invite.organization_id,
        email=invite.email,
        role=invite.role,
        status=invite.status,
        created_at=invite.created_at or datetime.utcnow(),
    )


@router.post("/api/invites/accept")
async def org_api_invite_accept(
    body: OrgInviteAcceptRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Consume an org_invite token, create the membership, and (if the
    invitee has no `users` row yet) create the user.

    The endpoint is intentionally PUBLIC — no `Depends` on session. The
    token IS the credential. 404 on any token mismatch (expired, used,
    invalid, or already consumed) — never reveals whether the token ever
    existed."""
    token = (body.token or "").strip()
    if not token:
        raise NotFoundError("Invite not found")

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    auth_token = (
        await db.execute(
            select(AuthToken).where(
                AuthToken.token_hash == token_hash,
                AuthToken.purpose == "org_invite",
            )
        )
    ).scalar_one_or_none()
    if not auth_token:
        raise NotFoundError("Invite not found")
    # Compare timezone-aware UTC values; `auth_tokens.expires_at` is UTC-naive
    # in the schema but stored as UTC by the issuer.
    expires = auth_token.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < datetime.now(UTC):
        raise NotFoundError("Invite not found")
    if auth_token.used_at is not None:
        raise NotFoundError("Invite not found")

    invite = await OrgInvitesRepository(db).get_by_auth_token_id(auth_token.id)
    if not invite or invite.status != "pending":
        raise NotFoundError("Invite not found")

    org = await OrganizationsRepository(db).get(invite.organization_id)
    if not org or org.deleted_at is not None:
        raise NotFoundError("Invite not found")

    users_repo = UsersRepository(db)
    user = await users_repo.get_by_email_active(invite.email)

    if user is None:
        # New user path — require password + display_name in the body.
        if not body.password:
            raise ValidationError(
                "Password required for new accounts", code="password_required",
            )
        user = User(
            email=invite.email,
            password_hash=hash_password(body.password),
            display_name=(body.display_name or invite.email.split("@")[0])[:200],
        )
        db.add(user)
        await db.flush()

    # Idempotent: re-accepting just promotes the existing row.
    uo_repo = UserOrganizationsRepository(db)
    existing_membership = await uo_repo.get_active(
        user_id=user.id, organization_id=invite.organization_id, role=invite.role,
    )
    if existing_membership is None:
        membership = UserOrganization(
            user_id=user.id,
            organization_id=invite.organization_id,
            role=invite.role,
            region_id=invite.region_id,
            branch_id=invite.branch_id,
            invited_by=invite.invited_by,
            invited_at=invite.created_at,
            accepted_at=datetime.utcnow(),
        )
        db.add(membership)
        await db.flush()

    invite.status = "accepted"
    auth_token.used_at = datetime.utcnow()
    await db.flush()

    await log_org_action(
        db,
        organization_id=invite.organization_id,
        actor_user_id=user.id,
        actor_email=user.email,
        action="user.invite.accept",
        target_type="org_invite",
        target_id=invite.id,
        request=request,
    )
    return {"ok": True, "redirect": "/org/login"}


__all__ = ["router"]
