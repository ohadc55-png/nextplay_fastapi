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


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


class OrgInviteRequest(BaseModel):
    """POST /org/api/orgs/{org_id}/users/invite."""
    email: EmailStr
    role: str = Field(min_length=1, max_length=30)
    region_id: int | None = None
    branch_id: int | None = None


class OrgInviteOut(BaseModel):
    """Response after issuing an invite. Does NOT include the raw token."""
    id: int
    organization_id: int
    email: str
    role: str
    status: str
    created_at: datetime


class OrgInviteAcceptRequest(BaseModel):
    """POST /org/api/invites/accept — sets password if invitee is new, then
    promotes the OrgInvite into a UserOrganization row."""
    token: str
    password: str | None = None  # required only if invitee user doesn't exist yet
    display_name: str | None = None


__all__ = [
    "DashboardSummary",
    "OrganizationOut",
    "OrgInviteAcceptRequest",
    "OrgInviteOut",
    "OrgInviteRequest",
    "OrgLoginRequest",
    "OrgMeResponse",
    "OrgRoleOption",
    "OrgRoleSelection",
]
