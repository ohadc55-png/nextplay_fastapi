"""Auth-flow request + response schemas.

Covers: register, login, refresh, logout, forgot/reset/change password,
email verification, OAuth callbacks. Token shape matches v1.0-flask
exactly so existing JWTs validate during the migration window.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

# ---------------------------------------------------------------------------
# Password validation — mirrors v1.0-flask backend/auth/utils.py:158
# ---------------------------------------------------------------------------

_PASSWORD_RE = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,128}$")


def _validate_password(value: str) -> str:
    if not _PASSWORD_RE.match(value):
        raise ValueError(
            "Password must be 8-128 characters and contain at least one "
            "uppercase letter, one lowercase letter, and one digit."
        )
    return value


# ---------------------------------------------------------------------------
# Register / Login
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str = Field(min_length=1, max_length=255)
    invite_code: str | None = None  # optional invite-code redeem

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _validate_password(v)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenPair(BaseModel):
    """Returned on login / refresh. Mirrors what v1.0-flask wrote into cookies."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshRequest(BaseModel):
    """Body for `/api/auth/refresh`. The refresh token is OPTIONAL because
    browser clients send it as a cookie; only mobile/API clients put it in
    the request body. The route handler reads cookie-first, body-fallback."""

    refresh_token: str | None = None


# ---------------------------------------------------------------------------
# Password reset / change
# ---------------------------------------------------------------------------

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str  # the AuthToken hash from the email link
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _validate_password(v)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _validate_password(v)


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

class VerifyEmailRequest(BaseModel):
    token: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------

class DeleteAccountRequest(BaseModel):
    """Soft-delete the current user. Optional password re-confirmation for
    accounts that have one (OAuth-only accounts skip)."""

    password: str | None = None


# ---------------------------------------------------------------------------
# Audit log shape (admin only)
# ---------------------------------------------------------------------------

class AuditLogResponse(BaseModel):
    id: int
    user_id: int | None
    action: str
    ip_address: str | None
    user_agent: str | None
    created_at: str

    model_config = {"from_attributes": True}


__all__ = [
    "RegisterRequest",
    "LoginRequest",
    "TokenPair",
    "RefreshRequest",
    "ForgotPasswordRequest",
    "ResetPasswordRequest",
    "ChangePasswordRequest",
    "VerifyEmailRequest",
    "ResendVerificationRequest",
    "DeleteAccountRequest",
    "AuditLogResponse",
]
