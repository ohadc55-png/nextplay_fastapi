"""Refresh-token issuance + rotation.

Tokens are random base64url strings stored in DB only as a SHA256 hash —
never plaintext. Mirrors v1.0-flask
`backend/auth/utils.py:create_refresh_token`/:validate/:revoke.

Layout: this module only generates + hashes tokens. The DB-side rows are
managed by `RefreshTokenRepository` from Phase 2.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from src.core.config import settings


def generate_refresh_token() -> tuple[str, str, datetime]:
    """Return `(raw_token, token_hash, expires_at)`.

    The CALLER must:
      1. Persist the row via `RefreshTokenRepository.create(...)` using the
         hash + expiry.
      2. Send the RAW token to the client (cookie or response body).
      3. Never log the raw token.

    The hash is SHA256 (not bcrypt) because refresh tokens are 64 bytes of
    URL-safe random data — already high-entropy. Bcrypt's salt/work factor
    don't add value when the input is already unguessable.
    """
    raw_token = secrets.token_urlsafe(64)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return raw_token, token_hash, expires_at


def hash_refresh_token(raw_token: str) -> str:
    """Deterministic hash for lookup. SHA256 (not bcrypt) — same reason as above."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


__all__ = ["generate_refresh_token", "hash_refresh_token"]
