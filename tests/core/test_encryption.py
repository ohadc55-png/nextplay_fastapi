"""Tests for src/core/encryption.py — Fernet round-trip + key rotation."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet


def test_round_trip(monkeypatch):
    """encrypt → decrypt returns the original plaintext, and the ciphertext
    is NOT visually similar to the plaintext."""
    from src.core.encryption import decrypt_str, encrypt_str

    plaintext = "Top secret: 040-1234567"
    ct = encrypt_str(plaintext)
    assert plaintext not in ct
    assert "040-1234567" not in ct
    assert decrypt_str(ct) == plaintext


def test_missing_key_raises(monkeypatch):
    """If ENCRYPTION_KEY is empty AND we re-build, the helper raises."""
    from src.core import encryption
    from src.core.config import settings

    # Clear the build cache so the next call re-reads settings.
    encryption._build_fernet.cache_clear()
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "")
    monkeypatch.setattr(settings, "ENCRYPTION_KEY_PREVIOUS", "")
    with pytest.raises(encryption.EncryptionKeyMissing):
        encryption.get_fernet()
    encryption._build_fernet.cache_clear()


def test_multifernet_rotation(monkeypatch):
    """A row written with key A is still readable after rotation
    (primary=B, previous=A). The new write uses key B (primary)."""
    from src.core import encryption
    from src.core.config import settings

    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()

    # Pre-rotation state: only key_a.
    encryption._build_fernet.cache_clear()
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", key_a)
    monkeypatch.setattr(settings, "ENCRYPTION_KEY_PREVIOUS", "")

    secret = "before-rotation"
    blob = encryption.encrypt_str(secret)

    # Post-rotation state: primary=key_b, previous=key_a. The MultiFernet
    # accepts old blobs (decrypts with key_a), but new writes go through key_b.
    encryption._build_fernet.cache_clear()
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", key_b)
    monkeypatch.setattr(settings, "ENCRYPTION_KEY_PREVIOUS", key_a)

    assert encryption.decrypt_str(blob) == secret
    new_blob = encryption.encrypt_str("post-rotation")
    assert encryption.decrypt_str(new_blob) == "post-rotation"

    # If we drop key_a (rotation script has re-written every row), the OLD
    # blob can no longer be decrypted — but the new one still can.
    encryption._build_fernet.cache_clear()
    monkeypatch.setattr(settings, "ENCRYPTION_KEY_PREVIOUS", "")
    assert encryption.decrypt_str(new_blob) == "post-rotation"
    with pytest.raises(encryption.EncryptionDecodeError):
        encryption.decrypt_str(blob)
    encryption._build_fernet.cache_clear()


def test_decrypt_garbage_raises():
    """A blob that wasn't produced by Fernet raises a DecodeError, not a
    cryptography-internal exception (we wrap for clean error handling)."""
    from src.core import encryption

    with pytest.raises(encryption.EncryptionDecodeError):
        encryption.decrypt_str("not-a-real-token")


def test_none_passthrough_via_typedecorator():
    """The EncryptedText TypeDecorator must pass None through both directions."""
    from src.core.encryption import EncryptedText

    t = EncryptedText()
    assert t.process_bind_param(None, None) is None
    assert t.process_result_value(None, None) is None
