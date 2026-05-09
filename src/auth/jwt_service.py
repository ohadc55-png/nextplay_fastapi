"""JWT access-token issuance + verification.

Token shape is byte-identical to v1.0-flask
(`backend/auth/utils.py:create_access_token` + `:decode_access_token`):

    {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "iat": iso datetime,
        "exp": iso datetime,
        "type": "access",
    }

Algorithm: HS256, secret from `settings.JWT_SECRET_KEY`. By keeping the
shape and algo identical, JWTs minted by v1.0-flask continue to validate
during the cutover window (a coach with a fresh access cookie doesn't have
to re-login when Railway flips to FastAPI).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from src.core.config import settings

_ALGORITHM = "HS256"


def create_access_token(*, user_id: int, email: str, role: str = "coach") -> str:
    """Mint a fresh access JWT. Mirrors v1.0-flask
    `backend/auth/utils.py:59`."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """Verify signature + expiry. Returns claims dict or None.

    Returning None (rather than raising) lets the dependency layer turn any
    failure into a uniform 401 without leaking the specific reason
    (expired vs forged vs malformed)."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[_ALGORITHM])
    except JWTError:
        return None
    if payload.get("type") != "access":
        return None
    return payload


__all__ = ["create_access_token", "decode_access_token"]
