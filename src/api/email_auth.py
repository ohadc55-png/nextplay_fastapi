"""Email-driven auth flows: verify email, resend verification, forgot
password, reset password.

All four endpoints are CSRF-exempt (the verify/reset flows are GET/POST
landings from email links — the request originates outside the SPA).
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.password_service import hash_password
from src.core.database import get_db
from src.core.exceptions import NotFoundError, UnauthorizedError
from src.repositories.auth_repo import AuditLogRepository, AuthTokenRepository
from src.repositories.users_repo import UsersRepository
from src.schemas.auth import (
    ForgotPasswordRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    VerifyEmailRequest,
)
from src.schemas.common import StatusResponse
from src.services.email_service import (
    send_password_reset_email,
    send_verification_email,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth-email"])


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _ip_and_ua(request: Request) -> tuple[str, str]:
    ip = (request.headers.get("x-forwarded-for") or request.client.host or "").split(",")[0].strip()
    ua = request.headers.get("user-agent", "")[:255]
    return ip, ua


# ---------------------------------------------------------------------------
# Verify email
# ---------------------------------------------------------------------------

@router.post("/verify-email", response_model=StatusResponse)
async def verify_email(
    body: VerifyEmailRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Consume a verify_email AuthToken and flip users.email_verified=1."""
    token_hash = _hash_token(body.token)
    auth_repo = AuthTokenRepository(db)
    row = await auth_repo.get_unused_by_hash(token_hash, purpose="verify_email")
    if not row:
        raise UnauthorizedError("Invalid or expired token", code="invalid_token")

    await UsersRepository(db).mark_email_verified(row.user_id)
    await auth_repo.mark_used(row.id)

    ip, ua = _ip_and_ua(request)
    await AuditLogRepository(db).add(
        user_id=row.user_id, action="email_verified", ip_address=ip, user_agent=ua,
    )
    return StatusResponse(status="ok", detail="email verified")


# ---------------------------------------------------------------------------
# Resend verification
# ---------------------------------------------------------------------------

@router.post("/resend-verification", response_model=StatusResponse)
async def resend_verification(
    body: ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
):
    """Issue a new verify_email token + log the email send. Always returns
    200 (don't leak whether the email is registered)."""
    user = await UsersRepository(db).get_by_email_active(body.email)
    if user and not user.email_verified:
        try:
            await send_verification_email(db, user_id=user.id, email=user.email)
        except Exception as e:  # noqa: BLE001
            logger.warning("[resend-verification] enqueue failed for %s: %s", body.email, e)
    return StatusResponse(status="ok", detail="if the email is registered, a verification link was sent")


# ---------------------------------------------------------------------------
# Forgot password
# ---------------------------------------------------------------------------

@router.post("/forgot-password", response_model=StatusResponse)
async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Issue a reset_password token. Always returns 200 — don't leak which
    emails exist (anti-enumeration)."""
    user = await UsersRepository(db).get_by_email_active(body.email)
    if user:
        try:
            await send_password_reset_email(db, user_id=user.id, email=user.email)
        except Exception as e:  # noqa: BLE001
            logger.warning("[forgot-password] enqueue failed for %s: %s", body.email, e)
    return StatusResponse(status="ok", detail="if the email is registered, a reset link was sent")


# ---------------------------------------------------------------------------
# Reset password
# ---------------------------------------------------------------------------

@router.post("/reset-password", response_model=StatusResponse)
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Consume reset_password token + update password + revoke all refresh
    tokens (force re-login on every device, including the one that
    initiated the reset)."""
    token_hash = _hash_token(body.token)
    auth_repo = AuthTokenRepository(db)
    row = await auth_repo.get_unused_by_hash(token_hash, purpose="reset_password")
    if not row:
        raise UnauthorizedError("Invalid or expired token", code="invalid_token")

    new_hash = hash_password(body.new_password)
    await UsersRepository(db).update_password(row.user_id, new_hash)
    await auth_repo.mark_used(row.id)

    # Revoke all refresh tokens — security-sensitive op forces re-login.
    from src.repositories.auth_repo import RefreshTokenRepository
    await RefreshTokenRepository(db).revoke_all_for_user(row.user_id)

    ip, ua = _ip_and_ua(request)
    await AuditLogRepository(db).add(
        user_id=row.user_id, action="password_reset", ip_address=ip, user_agent=ua,
    )
    return StatusResponse(status="ok", detail="password reset")
