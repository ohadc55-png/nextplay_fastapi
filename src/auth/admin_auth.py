"""Admin auth — bcrypt password verify + email allowlist.

Async port of the admin auth logic in `backend/admin/routes.py:30-73`.
The actual session storage is handled by Starlette's SessionMiddleware
(already wired in `src/main.py`); this module exposes the building
blocks the admin login route uses.

Fail-closed: if `ADMIN_PASSWORD_HASH` isn't configured, every login
attempt returns False. v1 logged a critical warning at boot; we surface
it via `is_admin_auth_configured()` so the route can return a clear
error instead of an opaque 401.
"""

from __future__ import annotations

import logging

import bcrypt

from src.core.config import settings

logger = logging.getLogger(__name__)


def is_admin_auth_configured() -> bool:
    """True iff ADMIN_PASSWORD_HASH is set in env. Mirrors the v1 startup
    check at `backend/admin/routes.py:39-44`."""
    return bool(settings.ADMIN_PASSWORD_HASH and settings.ADMIN_PASSWORD_HASH.strip())


def is_admin_email(email: str) -> bool:
    """Case-insensitive allowlist check against settings.ADMIN_EMAILS."""
    return (email or "").strip().lower() in {
        e.lower() for e in settings.admin_emails_list
    }


def verify_admin_password(password: str) -> bool:
    """Compare the user-supplied password against ADMIN_PASSWORD_HASH.
    Returns False (silently) if either input is missing — matches v1's
    fail-closed behavior. The bcrypt cost factor is whatever was used
    when the hash was generated; v1 docs recommend 12."""
    if not password or not is_admin_auth_configured():
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            settings.ADMIN_PASSWORD_HASH.strip().encode("utf-8"),
        )
    except (ValueError, TypeError) as e:
        # Malformed hash. Log loud and reject — we don't want a typo'd
        # env var to silently disable admin auth.
        logger.error("[admin] ADMIN_PASSWORD_HASH appears malformed: %s", e)
        return False


__all__ = ["is_admin_auth_configured", "is_admin_email", "verify_admin_password"]
