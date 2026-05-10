"""Auth orchestration — multi-repo flows for register / login / refresh.

Routers stay thin: they validate input + set cookies + return responses.
Anything that touches multiple repos (creating a user + redeeming an
invite + scheduling a verification email + issuing a token pair) lives
here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.jwt_service import create_access_token
from src.auth.password_service import hash_password
from src.auth.refresh_token_service import generate_refresh_token, hash_refresh_token
from src.core.exceptions import ForbiddenError, UnauthorizedError, ValidationError
from src.models.auth import RefreshToken
from src.models.users import User
from src.repositories.auth_repo import RefreshTokenRepository
from src.repositories.clubs_repo import InviteCodesRepository
from src.repositories.users_repo import UsersRepository
from src.services.email_service import send_verification_email

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token issuance helpers
# ---------------------------------------------------------------------------

async def issue_token_pair(
    session: AsyncSession, *, user: User, device_info: str = ""
) -> tuple[str, str]:
    """Mint an access + refresh token. Persists the refresh hash. Returns
    `(access_jwt, raw_refresh_token)` — the caller passes both to the cookie
    helper or returns them in the response body."""
    access = create_access_token(user_id=user.id, email=user.email, role=user.role)
    raw_refresh, token_hash, refresh_expires_at = generate_refresh_token()
    session.add(RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        device_info=device_info or "",
        expires_at=refresh_expires_at.isoformat(),
    ))
    await session.flush()
    return access, raw_refresh


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

async def register_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    invite_code: str | None = None,
) -> User:
    """Create a new user + redeem invite code if provided + schedule
    verification email. Returns the persisted User."""
    users_repo = UsersRepository(session)
    if await users_repo.get_by_email_active(email):
        raise ValidationError("Email already registered", code="email_taken")

    # Validate invite code BEFORE creating the user, so we don't create a
    # user that can't be linked properly.
    invite = None
    if invite_code:
        invites_repo = InviteCodesRepository(session)
        invite = await invites_repo.get_unredeemed_by_code(invite_code)
        if not invite:
            raise ValidationError("Invalid or already-redeemed invite code", code="invalid_code")
        if invite.email and invite.email.lower() != email.lower():
            raise ForbiddenError(
                "This invite code is locked to a different email", code="email_mismatch"
            )

    trial_ends = (datetime.now(UTC) + timedelta(days=14)).isoformat()
    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        email_verified=0,
        email_infra_signup=True,  # post-cutover signups must verify
        subscription_plan="trial",
        trial_ends_at=trial_ends,
    )
    session.add(user)
    await session.flush()

    # If invite code is valid, redeem it now (race-protected via guarded UPDATE)
    if invite is not None:
        ok = await InviteCodesRepository(session).mark_redeemed(
            code_id=invite.id, user_id=user.id,
        )
        if ok:
            user.subscription_plan = invite.plan or "pro"
            user.trial_ends_at = None
            if invite.club_id is not None:
                user.club_id = invite.club_id
            await session.flush()

    # Verification email (audit-trail row + log line; Resend dispatch in Phase 7).
    try:
        await send_verification_email(session, user_id=user.id, email=user.email)
    except Exception as e:
        logger.warning("[register] verification email enqueue failed: %s", e)

    return user


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def authenticate(
    session: AsyncSession, *, email: str, password: str
) -> User:
    """Verify credentials + enforce email-verification gate. Raises 401 on
    any failure. Returns the User on success.

    The post-cutover email-verification gate fires only for accounts with
    `email_infra_signup=TRUE` — legacy users are grandfathered."""
    from src.auth.password_service import verify_password

    users_repo = UsersRepository(session)
    user = await users_repo.get_by_email_active(email)
    if not user or not user.password_hash:
        raise UnauthorizedError("Invalid email or password")
    if not verify_password(password, user.password_hash):
        raise UnauthorizedError("Invalid email or password")
    if user.email_infra_signup and not user.email_verified:
        raise ForbiddenError("Email not verified", code="email_not_verified")
    if not user.is_active:
        raise UnauthorizedError("Account inactive")

    await users_repo.mark_logged_in(user.id)
    return user


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

async def rotate_refresh(
    session: AsyncSession, *, raw_refresh_token: str, device_info: str = ""
) -> tuple[User, str, str]:
    """Validate + rotate a refresh token. Returns `(user, new_access,
    new_refresh)`. Old refresh is revoked atomically before the new one is
    issued so a stolen token can't be used twice."""
    token_hash = hash_refresh_token(raw_refresh_token)
    rt_repo = RefreshTokenRepository(session)
    rt = await rt_repo.get_active_by_hash(token_hash)
    if not rt:
        raise UnauthorizedError("Refresh token invalid or revoked")

    # Check expiry — DB stores ISO string
    try:
        expires_at = datetime.fromisoformat(rt.expires_at)
    except (ValueError, TypeError):
        raise UnauthorizedError("Refresh token malformed") from None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if datetime.now(UTC) > expires_at:
        raise UnauthorizedError("Refresh token expired")

    users_repo = UsersRepository(session)
    user = await users_repo.get_active(rt.user_id)
    if not user or not user.is_active:
        raise UnauthorizedError("User no longer active")

    # Atomically revoke the old token, issue a new pair.
    await rt_repo.revoke(rt.id)
    access, new_raw = await issue_token_pair(session, user=user, device_info=device_info)
    return user, access, new_raw


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

async def revoke_refresh(session: AsyncSession, *, raw_refresh_token: str | None) -> None:
    """Best-effort: revoke this device's refresh token. Missing or invalid
    tokens are silently ignored — logout shouldn't error on already-stale state."""
    if not raw_refresh_token:
        return
    token_hash = hash_refresh_token(raw_refresh_token)
    rt = await RefreshTokenRepository(session).get_active_by_hash(token_hash)
    if rt:
        await RefreshTokenRepository(session).revoke(rt.id)


# ---------------------------------------------------------------------------
# Change password (authenticated)
# ---------------------------------------------------------------------------

async def change_password(
    session: AsyncSession,
    *,
    user: User,
    current_password: str,
    new_password: str,
) -> None:
    """Verify current password matches, then hash + persist the new one,
    then revoke all other refresh tokens (force re-login on other devices)."""
    from src.auth.password_service import verify_password

    if not user.password_hash or not verify_password(current_password, user.password_hash):
        raise UnauthorizedError("Current password incorrect")
    new_hash = hash_password(new_password)
    await UsersRepository(session).update_password(user.id, new_hash)
    await RefreshTokenRepository(session).revoke_all_for_user(user.id)


# ---------------------------------------------------------------------------
# Delete account (soft)
# ---------------------------------------------------------------------------

async def delete_account(
    session: AsyncSession, *, user: User
) -> None:
    """Soft-delete + revoke all refresh tokens. Account row is kept (the
    user can re-register the email later if they want)."""
    await UsersRepository(session).soft_delete(user.id)
    await RefreshTokenRepository(session).revoke_all_for_user(user.id)


__all__ = [
    "authenticate",
    "change_password",
    "delete_account",
    "issue_token_pair",
    "register_user",
    "revoke_refresh",
    "rotate_refresh",
]
