"""Pydantic shapes for /org/api/users/* (Phase 1.4 — User Management)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr

from src.schemas.common import ORMModel

# Mirror the role set in src/api/org.py:_VALID_ROLES.
OrgRole = Literal["org_admin", "region_manager", "branch_manager", "coach", "viewer"]


class OrgMemberOut(BaseModel):
    """One row in the /org/api/users list. The user table is joined client-side
    in the repository so we have email + display_name without a lazy load."""

    membership_id: int
    user_id: int
    email: str
    display_name: str | None = None
    role: OrgRole
    region_id: int | None = None
    branch_id: int | None = None
    status: str  # 'active' | 'suspended' | 'removed'
    invited_by: int | None = None
    invited_at: datetime | None = None
    accepted_at: datetime | None = None


class MemberInviteRequest(BaseModel):
    """org_admin (and region_manager scoped to own region) issues an invite.
    org_id is read from the active session — never from the body, to keep
    cross-org invites impossible from the front-end."""

    email: EmailStr
    role: OrgRole
    region_id: int | None = None
    branch_id: int | None = None


class MemberInviteOut(ORMModel):
    """Response after an invite has been issued. Never includes the raw
    auth_token — the token only ever leaves through the email body."""

    id: int
    organization_id: int
    email: str
    role: OrgRole
    status: str  # 'pending' | 'accepted' | 'cancelled'
    region_id: int | None = None
    branch_id: int | None = None
    created_at: datetime | None = None


class PendingInviteOut(MemberInviteOut):
    """List item for /org/api/users/invites/pending."""

    invited_by: int | None = None


class MemberRoleUpdate(BaseModel):
    """PATCH /org/api/users/{membership_id} — change role only."""

    role: OrgRole


class MemberScopeUpdate(BaseModel):
    """PATCH /org/api/users/{membership_id} — change region/branch scope.
    Validation rules (region_manager requires region_id; branch_manager
    requires branch_id; both must belong to the active org) live in the
    repository, not here."""

    region_id: int | None = None
    branch_id: int | None = None


__all__ = [
    "MemberInviteOut",
    "MemberInviteRequest",
    "MemberRoleUpdate",
    "MemberScopeUpdate",
    "OrgMemberOut",
    "OrgRole",
    "PendingInviteOut",
]
