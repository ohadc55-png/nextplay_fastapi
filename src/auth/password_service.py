"""Password hashing + verification (bcrypt, 12 rounds — same as v1.0-flask).

Bcrypt itself is sync + CPU-bound. For a single login request we don't need
to push to a thread pool — bcrypt at 12 rounds takes ~250ms on a typical
server, which fits comfortably within an async handler. Under load we can
reconsider.
"""

from __future__ import annotations

import bcrypt

_ROUNDS = 12


def hash_password(plaintext: str) -> str:
    """Mirror of v1.0-flask `backend/auth/utils.py:49`. Output is the bcrypt
    hash as utf-8 text (encoded with the salt + work factor inline)."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=_ROUNDS)).decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True if the bcrypt hash validates. Constant-time per bcrypt's
    own implementation — safe against timing attacks."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash (e.g. legacy plaintext leftover) → fail closed.
        return False


__all__ = ["hash_password", "verify_password"]
