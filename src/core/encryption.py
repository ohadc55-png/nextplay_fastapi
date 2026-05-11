"""Field-level encryption (Phase 1.6).

Symmetric Fernet (AES-128-CBC + HMAC-SHA256) via the cryptography library.
A single PRIMARY key encrypts new writes; an optional PREVIOUS key lets us
rotate without downtime — `MultiFernet` will decrypt with either, and the
one-shot rotation script (Sub-Phase 1.6) re-writes every row using the
primary so the previous can eventually be retired.

Used by `src/models/player_contacts.py` for parent_phone_enc, national_id_enc,
medical_notes_enc, address_enc — i.e. the columns that hold sensitive PII.
NOT used for anything searchable (parent_name, parent_email stay plaintext
because we need to invite/match on them).

The TypeDecorator is dialect-agnostic — works on SQLite (tests) and Postgres
(prod) alike. Mirrors the `JSONText` pattern in src/core/database.py.

Key management:
- `settings.ENCRYPTION_KEY` (required) — current primary; generated once with
  `Fernet.generate_key()` and saved to .env + Railway secret + password manager.
- `settings.ENCRYPTION_KEY_PREVIOUS` (optional) — set during rotation only.
- Losing the primary key = losing every encrypted column. There is no recovery.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from src.core.config import settings

logger = logging.getLogger(__name__)


class EncryptionKeyMissing(RuntimeError):
    """Raised when ENCRYPTION_KEY is empty at the moment a write/read is
    attempted. Indicates a misconfigured deployment — fail loud, never
    silently store plaintext."""


class EncryptionDecodeError(RuntimeError):
    """Raised when a stored ciphertext cannot be decoded. Either the key was
    rotated without re-writing the row, or the row was tampered with."""


@lru_cache(maxsize=4)
def _build_fernet(primary: str, previous: str) -> MultiFernet:
    """Build a MultiFernet from one or two keys, cached by the (primary,
    previous) tuple. If primary is empty, raise — we never want to fall
    back to a default in production.

    The lru_cache is keyed on the actual key strings so when settings
    change in tests (via monkeypatch) a new MultiFernet is built."""
    if not primary:
        raise EncryptionKeyMissing(
            "ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and save to .env + "
            "Railway secret + a password manager."
        )
    keys = [Fernet(primary.encode("ascii"))]
    if previous:
        keys.append(Fernet(previous.encode("ascii")))
    return MultiFernet(keys)


def get_fernet() -> MultiFernet:
    """Return the active MultiFernet, built from current settings. Hot path —
    relies on `_build_fernet`'s lru_cache for speed (single dict lookup)."""
    return _build_fernet(
        settings.ENCRYPTION_KEY.strip(),
        settings.ENCRYPTION_KEY_PREVIOUS.strip(),
    )


def encrypt_str(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns ASCII ciphertext suitable for
    storage in a TEXT column."""
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_str(ciphertext: str) -> str:
    """Decrypt a ciphertext string. Raises `EncryptionDecodeError` if the
    blob is malformed or unreadable with any current key."""
    try:
        return get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionDecodeError(
            "Could not decrypt — key may have been rotated without re-writing "
            "this row, or the ciphertext was modified."
        ) from exc


class EncryptedText(TypeDecorator):  # noqa: D101
    """TEXT column whose value is transparently encrypted at write and
    decrypted at read. Use for low-volume PII (phone, national id, medical
    notes); NOT for anything that must be searchable in SQL.

    SQLAlchemy 2.0 requires `cache_ok = True` for compiled-statement caching
    to work with custom TypeDecorators."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        return encrypt_str(value)

    def process_result_value(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        return decrypt_str(value)


__all__ = [
    "EncryptedText",
    "EncryptionDecodeError",
    "EncryptionKeyMissing",
    "decrypt_str",
    "encrypt_str",
    "get_fernet",
]
