"""Auth cookie helpers (set / clear access + refresh cookies).

Cookie names + attributes match v1.0-flask `flask_app.py:_set_auth_cookies`
exactly so existing browser sessions survive the cutover.

Notable defaults:
- HttpOnly (JS can't read)
- SameSite=Lax (sent on top-level GET cross-origin; not sent on cross-site POST)
- Secure ONLY in production (controlled by `RAILWAY_ENVIRONMENT`)
- Refresh-cookie path is `/api/auth` so it's only sent to refresh/logout
  routes, not on every request.
"""

from __future__ import annotations

from fastapi import Response

from src.api.deps.auth import ACCESS_COOKIE_NAME
from src.core.config import settings

REFRESH_COOKIE_NAME = "refresh_token"
REFRESH_COOKIE_PATH = "/api/auth"


def set_auth_cookies(response: Response, *, access_token: str, refresh_token: str) -> None:
    """Set both auth cookies in one call. Used by login + refresh routes."""
    secure = settings.is_production
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=access_token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        httponly=True,
        secure=secure,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )


def clear_auth_cookies(response: Response) -> None:
    """Used by logout + delete-account."""
    response.delete_cookie(key=ACCESS_COOKIE_NAME, path="/")
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)


__all__ = [
    "REFRESH_COOKIE_NAME",
    "REFRESH_COOKIE_PATH",
    "set_auth_cookies",
    "clear_auth_cookies",
]
