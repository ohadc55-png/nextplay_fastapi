"""Club + invite-code schemas."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field

from src.schemas.common import ORMModel
from src.schemas.users import ClubMemberSummary

# ---------------------------------------------------------------------------
# Clubs
# ---------------------------------------------------------------------------

class ClubResponse(ORMModel):
    id: int
    name: str
    subscription_plan: str | None = None
    max_seats: int | None = None
    pooled_storage_gb: int | None = None


class ClubInfoResponse(BaseModel):
    """`/api/club/info` — club details + member list."""

    club: ClubResponse
    members: list[ClubMemberSummary]
    seats_used: int
    seats_remaining: int


class ClubInviteRequest(BaseModel):
    """`/api/club/invite` — admin invites a coach by email."""

    email: EmailStr


# ---------------------------------------------------------------------------
# Invite codes
# ---------------------------------------------------------------------------

class InviteCodeCreate(BaseModel):
    """Admin creates a code. v1.0-flask: `/api/admin/invite-codes`."""

    email: EmailStr | None = None  # restrict redeem to this email if set
    plan: str | None = "pro"
    club_id: int | None = None


class InviteCodeResponse(ORMModel):
    id: int
    code: str
    email: str | None = None
    plan: str | None = None
    redeemed_by: int | None = None
    redeemed_at: str | None = None
    club_id: int | None = None
    created_at: str | None = None


class RedeemCodeRequest(BaseModel):
    """User-side redeem — `/api/redeem-code`."""

    code: str = Field(min_length=1, max_length=64)


__all__ = [
    "ClubInfoResponse",
    "ClubInviteRequest",
    "ClubResponse",
    "InviteCodeCreate",
    "InviteCodeResponse",
    "RedeemCodeRequest",
]
