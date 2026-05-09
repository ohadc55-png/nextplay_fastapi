"""Core auth router — register / login / refresh / logout / me / delete /
change-password.

Mounted at `/api/auth/*`. CSRF-exempt (the SPA can't set X-Requested-With
on the very first POST that brings it to life).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.auth.cookies import REFRESH_COOKIE_NAME, clear_auth_cookies, set_auth_cookies
from src.core.config import settings
from src.core.database import get_db
from src.core.exceptions import UnauthorizedError
from src.models.users import User
from src.repositories.auth_repo import AuditLogRepository
from src.schemas.auth import (
    ChangePasswordRequest,
    DeleteAccountRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
)
from src.schemas.common import StatusResponse
from src.schemas.users import UserMeResponse
from src.services.auth_service import (
    authenticate,
    change_password as svc_change_password,
    delete_account as svc_delete_account,
    issue_token_pair,
    register_user,
    revoke_refresh,
    rotate_refresh,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ip_and_ua(request: Request) -> tuple[str, str]:
    ip = (request.headers.get("x-forwarded-for") or request.client.host or "").split(",")[0].strip()
    ua = request.headers.get("user-agent", "")[:255]
    return ip, ua


def _device_info(request: Request) -> str:
    ua = request.headers.get("user-agent", "")
    return ua[:255]


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

@router.post("/register", response_model=UserMeResponse)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Create account, issue tokens, set cookies. Email-verification gate
    fires on next login (post-cutover signups have email_infra_signup=TRUE)."""
    user = await register_user(
        db,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        invite_code=body.invite_code,
    )
    access, refresh = await issue_token_pair(db, user=user, device_info=_device_info(request))
    set_auth_cookies(response, access_token=access, refresh_token=refresh)

    ip, ua = _ip_and_ua(request)
    await AuditLogRepository(db).add(
        user_id=user.id, action="register", ip_address=ip, user_agent=ua,
    )
    return user


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.post("/login", response_model=TokenPair)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    ip, ua = _ip_and_ua(request)
    try:
        user = await authenticate(db, email=body.email, password=body.password)
    except UnauthorizedError:
        # Audit-log the failed attempt before re-raising. Useful for
        # brute-force detection later.
        await AuditLogRepository(db).add(
            user_id=None, action="login_failed", ip_address=ip, user_agent=ua,
            details=f"email={body.email}",
        )
        raise

    access, refresh = await issue_token_pair(db, user=user, device_info=_device_info(request))
    set_auth_cookies(response, access_token=access, refresh_token=refresh)
    await AuditLogRepository(db).add(
        user_id=user.id, action="login", ip_address=ip, user_agent=ua,
    )

    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

@router.post("/refresh", response_model=TokenPair)
async def refresh(
    request: Request,
    response: Response,
    body: RefreshRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Refresh tokens come from EITHER the request body (mobile) OR the
    refresh_token cookie (browser). The cookie wins if both are present."""
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw and body is not None:
        raw = body.refresh_token
    if not raw:
        raise UnauthorizedError("Refresh token required")

    user, access, new_refresh = await rotate_refresh(
        db, raw_refresh_token=raw, device_info=_device_info(request),
    )
    set_auth_cookies(response, access_token=access, refresh_token=new_refresh)
    return TokenPair(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.post("/logout", response_model=StatusResponse)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Best-effort: revoke this device's refresh token + clear cookies. Always
    returns 200 even if the cookie was missing — logout is idempotent."""
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    await revoke_refresh(db, raw_refresh_token=raw)
    clear_auth_cookies(response)
    ip, ua = _ip_and_ua(request)
    await AuditLogRepository(db).add(
        user_id=None, action="logout", ip_address=ip, user_agent=ua,
    )
    return StatusResponse(status="ok", detail="logged out")


# ---------------------------------------------------------------------------
# Me
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserMeResponse)
async def me(user: User = Depends(get_current_user)):
    return user


# ---------------------------------------------------------------------------
# Delete account (soft)
# ---------------------------------------------------------------------------

@router.delete("/delete-account", response_model=StatusResponse)
async def delete_account(
    body: DeleteAccountRequest | None = None,
    request: Request = None,
    response: Response = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete the current user. For password-account users, an optional
    confirmation password can be required (mirrors v1.0-flask)."""
    # If the account has a password set, optionally re-confirm it.
    if user.password_hash and body is not None and body.password:
        from src.auth.password_service import verify_password
        if not verify_password(body.password, user.password_hash):
            raise UnauthorizedError("Password incorrect")
    await svc_delete_account(db, user=user)
    if response is not None:
        clear_auth_cookies(response)
    if request is not None:
        ip, ua = _ip_and_ua(request)
        await AuditLogRepository(db).add(
            user_id=user.id, action="account_deleted", ip_address=ip, user_agent=ua,
        )
    return StatusResponse(status="ok", detail="account deleted")


# ---------------------------------------------------------------------------
# Change password (authenticated)
# ---------------------------------------------------------------------------

@router.post("/change-password", response_model=StatusResponse)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify current password, then update + revoke all OTHER refresh
    tokens (force re-login on every other device)."""
    await svc_change_password(
        db, user=user,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    # Re-issue THIS device's tokens so the user stays logged in here.
    access, refresh_tok = await issue_token_pair(db, user=user, device_info=_device_info(request))
    set_auth_cookies(response, access_token=access, refresh_token=refresh_tok)
    ip, ua = _ip_and_ua(request)
    await AuditLogRepository(db).add(
        user_id=user.id, action="password_changed", ip_address=ip, user_agent=ua,
    )
    return StatusResponse(status="ok", detail="password changed")
