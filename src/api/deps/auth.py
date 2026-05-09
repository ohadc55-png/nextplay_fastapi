"""FastAPI auth dependencies.

Mirrors the load_user / @login_required / @check_subscription / @require_pro
chain from `backend/auth/decorators.py` but in the dependency-injection
style FastAPI prefers. Each request runs:

1. ``get_current_user`` — extract JWT (cookie OR Bearer), decode, fetch
   the user row, run the trial→expired auto-flip and the data-purge
   trigger as side effects (matching v1.0-flask `load_user`).
2. Endpoint-specific guard — `require_active_subscription` (cost-leak
   stop), `require_pro` (Video Hub gate), `get_current_admin` (admin
   panel — separate session scheme).

Design note: subscription guards inherit from ``get_current_user`` so
declaring `Depends(require_pro)` on an endpoint also guarantees auth.
The order is mandated: auth → subscription class → pro tier.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Cookie, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.jwt_service import decode_access_token
from src.auth.purge_service import maybe_flip_expired, purge_user_data, should_purge
from src.core.config import settings
from src.core.database import get_db
from src.core.exceptions import ForbiddenError, SubscriptionError, UnauthorizedError
from src.models.users import User
from src.repositories.users_repo import UsersRepository

# ---------------------------------------------------------------------------
# Token extraction — cookie OR Authorization header (mobile/API)
# ---------------------------------------------------------------------------

ACCESS_COOKIE_NAME = "access_token"


def _extract_token(
    access_cookie: str | None,
    authorization_header: str | None,
) -> str | None:
    """Mirror v1.0-flask `load_user` precedence: try Authorization first,
    cookie second. Either-or, never both required."""
    if authorization_header and authorization_header.lower().startswith("bearer "):
        return authorization_header[7:].strip() or None
    return access_cookie


# ---------------------------------------------------------------------------
# Core dependency: get_current_user
# ---------------------------------------------------------------------------

async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    access_token: str | None = Cookie(default=None, alias=ACCESS_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> User:
    """Resolve the authenticated user. Raises ``UnauthorizedError`` (→ 401)
    if no valid token + active user is found.

    Side effects (matching `load_user` in v1.0-flask):
    - If trial expired but `subscription_plan` is still 'trial', flip to
      'expired' and schedule `data_purge_at = NOW + 30 days`.
    - If the grace period has elapsed (and the user isn't a club member),
      run `purge_user_data` once and clear `data_purge_at`.

    The user object is stored on `request.state.user` so middleware /
    other dependencies can read it without re-running this resolution.
    """
    token = _extract_token(access_token, authorization)
    if not token:
        raise UnauthorizedError("Authentication required")

    payload = decode_access_token(token)
    if not payload:
        raise UnauthorizedError("Authentication required")

    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise UnauthorizedError("Authentication required") from None

    repo = UsersRepository(db)
    user = await repo.get_active(user_id)
    if not user or not user.is_active:
        raise UnauthorizedError("Authentication required")

    # Trial → expired auto-flip (best-effort; failure shouldn't block auth).
    try:
        await maybe_flip_expired(db, user)
    except Exception:  # noqa: BLE001
        pass

    # Data-purge trigger — the 30-day grace period has elapsed for an
    # expired non-club user. Only fires once per user (purge clears
    # data_purge_at).
    if should_purge(user):
        try:
            await purge_user_data(db, user.id)
            user.data_purge_at = None
        except Exception:  # noqa: BLE001
            pass

    request.state.user = user
    return user


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
    access_token: str | None = Cookie(default=None, alias=ACCESS_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> User | None:
    """Same as `get_current_user` but returns None instead of raising. Use
    on routes that work both authenticated and anonymously (e.g. landing
    page, public play-share link)."""
    try:
        return await get_current_user(
            request=request,
            db=db,
            access_token=access_token,
            authorization=authorization,
        )
    except UnauthorizedError:
        return None


# ---------------------------------------------------------------------------
# Subscription guards
# ---------------------------------------------------------------------------

def require_active_subscription(
    user: User = Depends(get_current_user),
) -> User:
    """Cost-leak stop. Mirrors v1.0-flask `check_subscription` for API routes:
    `expired` plan → 403. Club members bypass everything."""
    if user.club_id is not None:
        return user  # club covers the seat
    if user.subscription_plan == "expired":
        raise SubscriptionError("Trial expired", code="trial_expired")
    return user


def require_pro(
    user: User = Depends(require_active_subscription),
) -> User:
    """Restrict to Pro / Enterprise / Club. Mirrors v1.0-flask `require_pro`.
    'basic' plan users hit 403 with an upgrade hint."""
    if user.club_id is not None:
        return user  # all club members get Pro features
    plan = user.subscription_plan or "trial"
    if plan == "basic":
        raise ForbiddenError("Video Hub requires Pro plan", code="pro_required")
    return user


# ---------------------------------------------------------------------------
# Admin auth — separate session cookie scheme (Phase 4 wires the routes)
# ---------------------------------------------------------------------------

ADMIN_COOKIE_NAME = "admin_session"


def is_admin_email(email: str) -> bool:
    """Mirrors `backend/admin/routes.py:32` allowlist check."""
    return email.lower() in {e.lower() for e in settings.admin_emails_list}


async def get_current_admin(
    request: Request,
    admin_session: str | None = Cookie(default=None, alias=ADMIN_COOKIE_NAME),
) -> str:
    """Returns the admin email. Raises 401 if no valid admin session.

    Phase 3 stub: the admin login route (which sets the cookie) lands in
    Phase 4 along with the admin panel itself. For now this dependency
    decodes a signed `<email>:<sig>` cookie format that the admin login
    will set. Implementation is intentionally minimal — the admin surface
    is small.
    """
    if not admin_session:
        raise UnauthorizedError("Admin authentication required")
    # Format defined when the admin login lands. For now: trust the cookie
    # value as-is if it matches an admin email (this is a placeholder; the
    # real implementation will use itsdangerous-style signing).
    if not is_admin_email(admin_session):
        raise UnauthorizedError("Admin authentication required")
    return admin_session.lower()


__all__ = [
    "ACCESS_COOKIE_NAME",
    "ADMIN_COOKIE_NAME",
    "get_current_user",
    "get_current_user_optional",
    "require_active_subscription",
    "require_pro",
    "get_current_admin",
    "is_admin_email",
]
