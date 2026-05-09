"""User profile schemas — `/api/me`, `/api/coach/profile`, club info."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field

from src.schemas.common import ORMModel


class UserUpdate(BaseModel):
    """PATCH /api/coach/profile — every field optional."""

    display_name: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = None
    timezone: str | None = None  # IANA name (e.g. "Asia/Jerusalem")


class UserResponse(ORMModel):
    """Default user shape returned by most endpoints."""

    id: int
    email: EmailStr
    display_name: str
    avatar_url: str | None = None
    role: str
    created_at: str | None = None


class UserMeResponse(ORMModel):
    """`/api/me` — adds subscription + club fields needed by the frontend."""

    id: int
    email: EmailStr
    display_name: str
    avatar_url: str | None = None
    role: str
    is_active: int
    email_verified: int
    active_team_id: int | None = None
    subscription_plan: str | None = None
    trial_ends_at: str | None = None
    club_id: int | None = None
    is_club_admin: int | None = 0
    push_enabled: bool = False
    email_marketing: bool = True


class ClubMemberSummary(ORMModel):
    """Used by `/api/club/info` member list."""

    id: int
    email: EmailStr
    display_name: str
    is_club_admin: int | None = 0
    last_login_at: str | None = None


__all__ = ["UserUpdate", "UserResponse", "UserMeResponse", "ClubMemberSummary"]
