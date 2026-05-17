"""Org Admin (`/org/*`) request + response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# ---------------------------------------------------------------------------
# Auth — login / role selection / current user
# ---------------------------------------------------------------------------


class OrgLoginRequest(BaseModel):
    email: EmailStr
    password: str


class OrgRoleSelection(BaseModel):
    """Body for POST /org/switch-role and the second step of multi-role login."""
    organization_id: int
    role: str


class OrgRoleOption(BaseModel):
    """One row in the role-selector list returned to multi-role users at login."""
    organization_id: int
    organization_name: str
    organization_slug: str
    role: str
    region_id: int | None = None
    branch_id: int | None = None


class OrgMeResponse(BaseModel):
    """GET /org/api/me — current user + active org/role."""
    user_id: int
    email: str
    display_name: str
    organization_id: int
    organization_name: str
    organization_slug: str
    role: str
    region_id: int | None = None
    branch_id: int | None = None


# ---------------------------------------------------------------------------
# Organizations / regions / branches (read-only in Phase 0)
# ---------------------------------------------------------------------------


class OrganizationOut(BaseModel):
    """A single org as returned to org members. NEVER includes other orgs."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    status: str
    plan: str


class DashboardSummary(BaseModel):
    """GET /org/api/dashboard/summary — header tiles for the basic dashboard."""
    organization: OrganizationOut
    role: str
    member_count: int
    branch_count: int
    region_count: int
    team_count: int
    # Phase 3+: program name surfaces in the header for program_manager and
    # region_manager (so a coach inside Sha'ar Shivyon sees "עמותת שער שיוויון · בועטות").
    program_id: int | None = None
    program_name: str | None = None


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


class OrgInviteRequest(BaseModel):
    """POST /org/api/orgs/{org_id}/users/invite + the newer
    POST /org/api/users/invite shared body schema.

    Phase 14 — `team_id` is set ONLY when inviting a coach. The PM/RM
    picks which team the new coach will own. At redeem time the team's
    `user_id` is rewritten to the new coach (replacing any existing one).
    The schema doesn't enforce role-vs-team_id consistency — that's the
    service layer's job (with friendlier error messages).
    """
    email: EmailStr
    role: str = Field(min_length=1, max_length=30)
    program_id: int | None = None
    region_id: int | None = None
    branch_id: int | None = None
    team_id: int | None = None


class OrgInviteOut(BaseModel):
    """Response after issuing an invite. Does NOT include the raw token.
    `short_code` IS included — the inviter copies it to share via WhatsApp/SMS."""
    id: int
    organization_id: int
    email: str
    role: str
    status: str
    short_code: str | None = None
    created_at: datetime


class OrgInviteAcceptRequest(BaseModel):
    """POST /org/api/invites/accept — sets password if invitee is new, then
    promotes the OrgInvite into a UserOrganization row."""
    token: str
    password: str | None = None  # required only if invitee user doesn't exist yet
    display_name: str | None = None


class OrgInviteRedeemRequest(BaseModel):
    """POST /org/api/invites/redeem — public self-service flow.

    Unlike the accept-token path (which locks the new user to invite.email),
    redemption with a short code accepts any email + name + password and
    creates the user de novo. The short_code is one-time-use; redeeming it
    also kills the magic-link token tied to the same OrgInvite.

    `email` is `str` (not `EmailStr`) on purpose — pydantic's strict email
    validator rejects `.test`/`.local` domains and would block dev fixtures.
    The route does a minimal "@ + ." sanity check; the User model already
    enforces uniqueness, and password reset / email verification provide
    additional liveness signals when needed.
    """
    code: str = Field(min_length=4, max_length=24)
    email: str = Field(min_length=3, max_length=320)
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=128)


__all__ = [
    "DashboardSummary",
    "OrganizationOut",
    "OrgInviteAcceptRequest",
    "OrgInviteOut",
    "OrgInviteRedeemRequest",
    "OrgInviteRequest",
    "OrgLoginRequest",
    "OrgMeResponse",
    "OrgRoleOption",
    "OrgRoleSelection",
]
