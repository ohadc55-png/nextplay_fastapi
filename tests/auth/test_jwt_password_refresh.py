"""Tests for JWT, password, and refresh-token services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from src.auth.jwt_service import create_access_token, decode_access_token
from src.auth.password_service import hash_password, verify_password
from src.auth.refresh_token_service import generate_refresh_token, hash_refresh_token
from src.core.config import settings

# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

class TestJWT:
    def test_round_trip(self):
        # Need a JWT secret for encode/decode
        with patch.object(settings, "JWT_SECRET_KEY", "test-secret-key-32-chars-long-xx"):
            token = create_access_token(user_id=42, email="c@x.com", role="coach")
            claims = decode_access_token(token)
        assert claims is not None
        assert claims["sub"] == "42"
        assert claims["email"] == "c@x.com"
        assert claims["role"] == "coach"
        assert claims["type"] == "access"

    def test_decode_rejects_wrong_type(self):
        """A token with type != 'access' (e.g. an old refresh-style JWT)
        must not validate as an access token."""
        from jose import jwt
        with patch.object(settings, "JWT_SECRET_KEY", "test-secret-key-32-chars-long-xx"):
            payload = {
                "sub": "1",
                "email": "x@x.com",
                "role": "coach",
                "type": "refresh",  # wrong type
                "exp": datetime.now(UTC) + timedelta(hours=1),
            }
            token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm="HS256")
            assert decode_access_token(token) is None

    def test_decode_rejects_tampered(self):
        with patch.object(settings, "JWT_SECRET_KEY", "test-secret-key-32-chars-long-xx"):
            token = create_access_token(user_id=1, email="x@x.com")
        # Flip the last char to break the signature
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        with patch.object(settings, "JWT_SECRET_KEY", "test-secret-key-32-chars-long-xx"):
            assert decode_access_token(tampered) is None

    def test_decode_rejects_expired(self):
        """Mint a token already in the past and confirm decode fails."""
        from jose import jwt
        with patch.object(settings, "JWT_SECRET_KEY", "test-secret-key-32-chars-long-xx"):
            past = datetime.now(UTC) - timedelta(hours=1)
            payload = {
                "sub": "1",
                "email": "x@x.com",
                "role": "coach",
                "type": "access",
                "iat": past - timedelta(hours=1),
                "exp": past,
            }
            token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm="HS256")
            assert decode_access_token(token) is None


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------

class TestPassword:
    def test_round_trip(self):
        h = hash_password("Sup3rSecure!")
        assert verify_password("Sup3rSecure!", h) is True
        assert verify_password("wrong", h) is False

    def test_verify_against_empty_hash_returns_false(self):
        assert verify_password("anything", "") is False

    def test_verify_against_malformed_hash_returns_false(self):
        # Not a valid bcrypt hash; must return False (not raise)
        assert verify_password("anything", "definitely-not-bcrypt") is False

    def test_hash_is_unique_per_call(self):
        """Bcrypt salts the hash; two calls with the same password produce
        different hashes (both verifying)."""
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2
        assert verify_password("same", h1) is True
        assert verify_password("same", h2) is True


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------

class TestRefreshToken:
    def test_generate_returns_distinct_tokens(self):
        a, _, _ = generate_refresh_token()
        b, _, _ = generate_refresh_token()
        assert a != b
        # secrets.token_urlsafe(64) produces 86-char base64url strings
        assert len(a) >= 80

    def test_hash_is_deterministic(self):
        raw, h1, _ = generate_refresh_token()
        h2 = hash_refresh_token(raw)
        assert h1 == h2

    def test_expiry_is_in_the_future(self):
        _, _, expires = generate_refresh_token()
        assert expires > datetime.now(UTC)
        # Within the configured window (default 30d)
        delta = expires - datetime.now(UTC)
        assert delta <= timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS + 1)
