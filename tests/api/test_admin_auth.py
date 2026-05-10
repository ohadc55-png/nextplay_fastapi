"""Admin auth — login / logout / me + ADMIN_PASSWORD_HASH fail-closed.

Coverage:
  - login with right creds → 200 + session cookie set
  - login with wrong password → 401
  - login with email NOT in ADMIN_EMAILS → 401
  - login when ADMIN_PASSWORD_HASH unset → 503 (fail-closed)
  - /admin/me requires login (401 without session)
  - logout pops the session
"""

from __future__ import annotations

from unittest.mock import patch

import bcrypt
import pytest_asyncio
from httpx import AsyncClient

from src.core.config import settings

ADMIN_EMAIL = "admin@test.com"
ADMIN_PASSWORD = "AdminPass1"


@pytest_asyncio.fixture
def admin_password_hash() -> str:
    """Generate a fresh bcrypt hash per test run. Cost factor 4 (down from
    12) keeps the test suite fast — the hash is only used to verify the
    same password we just hashed."""
    return bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest_asyncio.fixture
async def admin_env(admin_password_hash: str):
    """Patch the two admin env settings for the duration of a test."""
    with patch.object(settings, "ADMIN_PASSWORD_HASH", admin_password_hash), \
         patch.object(settings, "ADMIN_EMAILS", ADMIN_EMAIL):
        yield


class TestAdminLogin:
    async def test_correct_creds_returns_200_and_sets_session(
        self, api_client: AsyncClient, admin_env
    ):
        r = await api_client.post(
            "/admin/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "email": ADMIN_EMAIL}
        # Starlette SessionMiddleware sets a `session` cookie on the response.
        assert "session" in api_client.cookies

    async def test_wrong_password_returns_401(
        self, api_client: AsyncClient, admin_env
    ):
        r = await api_client.post(
            "/admin/login",
            json={"email": ADMIN_EMAIL, "password": "wrong"},
        )
        assert r.status_code == 401

    async def test_email_not_in_allowlist_returns_401(
        self, api_client: AsyncClient, admin_env
    ):
        r = await api_client.post(
            "/admin/login",
            json={"email": "stranger@test.com", "password": ADMIN_PASSWORD},
        )
        assert r.status_code == 401

    async def test_unconfigured_admin_hash_fails_closed_with_503(
        self, api_client: AsyncClient
    ):
        with patch.object(settings, "ADMIN_PASSWORD_HASH", ""):
            r = await api_client.post(
                "/admin/login",
                json={"email": ADMIN_EMAIL, "password": "anything"},
            )
        assert r.status_code == 503


class TestAdminMe:
    async def test_anon_request_to_me_returns_401(self, api_client: AsyncClient):
        r = await api_client.get("/admin/me")
        assert r.status_code == 401

    async def test_after_login_me_returns_email(
        self, api_client: AsyncClient, admin_env
    ):
        await api_client.post(
            "/admin/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        r = await api_client.get("/admin/me")
        assert r.status_code == 200
        assert r.json()["email"] == ADMIN_EMAIL


class TestAdminLogout:
    async def test_logout_clears_session(
        self, api_client: AsyncClient, admin_env
    ):
        await api_client.post(
            "/admin/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        r = await api_client.post("/admin/logout")
        assert r.status_code == 200
        # /admin/me now 401
        r2 = await api_client.get("/admin/me")
        assert r2.status_code == 401

    async def test_logout_when_not_logged_in_is_idempotent(
        self, api_client: AsyncClient
    ):
        r = await api_client.post("/admin/logout")
        assert r.status_code == 200
        assert r.json()["ok"] is True
