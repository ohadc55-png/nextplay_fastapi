"""Signing-session cookie helpers — Phase 2.3.

After a parent verifies their OTP, we set an opaque cookie that the
final submit endpoint checks. The cookie is HMAC-SHA256 signed over
`(delivery_token, expires_at_iso)` using `settings.SESSION_SECRET_KEY`.

Stateless on purpose — no DB lookup on submit. The cookie itself proves
"this browser verified an OTP for THIS token before <ts>". A stolen
cookie would only work for ONE specific delivery (different tokens get
different signatures), so the blast radius is bounded to that one form.

Format: `<delivery_token>.<expires_at_iso>.<hmac_hex>`. We use a dot as
separator (URL-safe) and the raw delivery_token is already 32 hex chars
(safe).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from src.core.config import settings

DEFAULT_TTL_SECONDS = 30 * 60  # 30 min — matches the plan


def _secret() -> bytes:
    """Pick the HMAC secret. Falls back to JWT_SECRET_KEY if SESSION isn't
    set (the project's existing convention; see auth/cookies.py)."""
    s = (settings.SESSION_SECRET_KEY or settings.JWT_SECRET_KEY or "").encode("utf-8")
    # Defensive: never sign with the empty string — that would let anyone
    # forge a cookie. The settings layer should already prevent this in prod.
    if not s:
        raise RuntimeError(
            "SESSION_SECRET_KEY (or JWT_SECRET_KEY) must be configured for signing sessions."
        )
    return s


def _hmac(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue(delivery_token: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint a fresh signing-session cookie value for `delivery_token`."""
    expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).replace(microsecond=0)
    exp_iso = expires.isoformat().replace("+00:00", "Z")
    payload = f"{delivery_token}.{exp_iso}"
    return f"{payload}.{_hmac(payload)}"


def verify(cookie_value: str | None, delivery_token: str) -> bool:
    """Return True iff `cookie_value` is well-formed, signed with our
    secret, bound to `delivery_token`, and not yet expired.

    Constant-time compare on the HMAC half so timing attacks can't probe
    the secret. All other checks short-circuit because they're not secret.
    """
    if not cookie_value or not delivery_token:
        return False
    parts = cookie_value.split(".")
    if len(parts) != 3:
        return False
    token, exp_iso, sig = parts
    if token != delivery_token:
        return False
    expected = _hmac(f"{token}.{exp_iso}")
    if not hmac.compare_digest(expected, sig):
        return False
    # Parse the expiry — invalid ISO → reject.
    try:
        exp = datetime.fromisoformat(exp_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    return exp > datetime.now(UTC)


__all__ = ["DEFAULT_TTL_SECONDS", "issue", "verify"]
