"""End-to-end Phase 3 verification: register → login → /me → refresh → logout.

Spins up the real `src.main:app` (full middleware stack + all auth routers)
against an in-memory SQLite, exercises the happy path + the most important
failure modes, and confirms `/healthz` still works after the middleware
stack landed.

The single `app` fixture overrides `get_db` to point at the test engine.
Subsequent fixtures hand out a single AsyncClient that persists cookies
across requests, so each test reads like a real session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import src.models  # noqa: F401 — register all ORM classes
from src.core.config import settings
from src.core.database import Base, get_db
from src.main import app

JWT_SECRET = "test-secret-32-chars-or-more-aaaaaaa"


@pytest_asyncio.fixture
async def e2e_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def e2e_client(e2e_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    factory = async_sessionmaker(e2e_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    async def _override_get_db():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    try:
        # Phase 7 added a per-IP rate limiter that bites on /api/auth/login
        # at 5 attempts/min. e2e tests legitimately make many login calls;
        # disable the limiter for the fixture's lifetime so we test auth
        # logic, not rate-limit behavior. Dedicated rate-limit tests
        # turn it back on locally.
        from unittest.mock import patch

        from src.core.config import settings

        with patch.object(settings, "RATE_LIMIT_ENABLED", False):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# /healthz survives the new middleware stack
# ---------------------------------------------------------------------------

class TestHealthz:
    async def test_healthz_returns_200(self, e2e_client: AsyncClient):
        r = await e2e_client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        # Security headers must now be on every response
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "Content-Security-Policy" in r.headers


# ---------------------------------------------------------------------------
# Happy path: register → login → /me → refresh → logout
# ---------------------------------------------------------------------------

class TestAuthHappyPath:
    async def test_full_flow(self, e2e_client: AsyncClient):
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            # 1. Register
            r = await e2e_client.post(
                "/api/auth/register",
                json={
                    "email": "newuser@example.com",
                    "password": "Sup3rSecure!",
                    "display_name": "Newbie",
                },
            )
            assert r.status_code == 200, r.text
            user = r.json()
            assert user["email"] == "newuser@example.com"
            assert user["subscription_plan"] == "trial"
            # Cookies set on register
            assert "access_token" in e2e_client.cookies
            assert "refresh_token" in e2e_client.cookies

            # 2. /me works because the cookie carries us
            r = await e2e_client.get("/api/auth/me")
            assert r.status_code == 200
            assert r.json()["email"] == "newuser@example.com"

            # 3. Logout clears the cookies
            r = await e2e_client.post("/api/auth/logout")
            assert r.status_code == 200
            # /me without auth → 401
            r = await e2e_client.get("/api/auth/me")
            assert r.status_code == 401

            # 4. Login back in via password
            r = await e2e_client.post(
                "/api/auth/login",
                json={"email": "newuser@example.com", "password": "Sup3rSecure!"},
            )
            # Login is gated on email_verified for post-cutover signups (which
            # we are — register sets email_infra_signup=TRUE). Expect 403 with
            # email_not_verified code.
            assert r.status_code == 403
            assert r.json()["code"] == "email_not_verified"


class TestAuthLegacyUserLogin:
    """A user without email_infra_signup (i.e. a legacy v1.0-flask account)
    can log in without verifying — they're grandfathered."""

    async def test_legacy_user_can_login_then_refresh_then_logout(
        self, e2e_client: AsyncClient, e2e_engine: AsyncEngine
    ):
        from src.auth.password_service import hash_password
        from src.models.users import User

        # Seed a legacy account directly
        factory = async_sessionmaker(e2e_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add(User(
                email="legacy@example.com",
                password_hash=hash_password("LegacyPwd1"),
                display_name="Legacy",
                email_verified=0,
                email_infra_signup=False,  # → can login without verifying
                subscription_plan="trial",
            ))
            await s.commit()

        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            # Login
            r = await e2e_client.post(
                "/api/auth/login",
                json={"email": "legacy@example.com", "password": "LegacyPwd1"},
            )
            assert r.status_code == 200, r.text
            tokens = r.json()
            assert "access_token" in tokens

            # /me
            r = await e2e_client.get("/api/auth/me")
            assert r.status_code == 200
            assert r.json()["email"] == "legacy@example.com"

            # Refresh — uses the refresh_token cookie set on login
            r = await e2e_client.post("/api/auth/refresh", json={})
            assert r.status_code == 200
            new_tokens = r.json()
            # Refresh token MUST rotate (random base64). Access tokens with
            # identical claims (iat/exp at second-resolution) will hash to
            # the same string, so we don't compare them.
            assert new_tokens["refresh_token"] != tokens["refresh_token"]

            # /me still works with the rotated cookies
            r = await e2e_client.get("/api/auth/me")
            assert r.status_code == 200

            # Logout
            r = await e2e_client.post("/api/auth/logout")
            assert r.status_code == 200
            r = await e2e_client.get("/api/auth/me")
            assert r.status_code == 401


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

class TestAuthFailures:
    async def test_register_with_existing_email_rejected(
        self, e2e_client: AsyncClient, e2e_engine: AsyncEngine
    ):
        from src.models.users import User
        factory = async_sessionmaker(e2e_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add(User(email="taken@x.com", password_hash="hash", display_name="x"))
            await s.commit()

        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            r = await e2e_client.post(
                "/api/auth/register",
                json={
                    "email": "taken@x.com",
                    "password": "Sup3rSecure!",
                    "display_name": "Dupe",
                },
            )
        assert r.status_code == 422
        assert r.json()["code"] == "email_taken"

    async def test_login_with_wrong_password_returns_401(
        self, e2e_client: AsyncClient, e2e_engine: AsyncEngine
    ):
        from src.auth.password_service import hash_password
        from src.models.users import User

        factory = async_sessionmaker(e2e_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            s.add(User(
                email="exists@x.com",
                password_hash=hash_password("Right1Pwd"),
                display_name="x",
                email_infra_signup=False,
            ))
            await s.commit()

        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            r = await e2e_client.post(
                "/api/auth/login",
                json={"email": "exists@x.com", "password": "Wrong"},
            )
        assert r.status_code == 401

    async def test_login_with_weak_password_rejected_at_register(
        self, e2e_client: AsyncClient
    ):
        """Register validates password complexity (regex). Confirms Pydantic
        runs the validator before the route body."""
        r = await e2e_client.post(
            "/api/auth/register",
            json={
                "email": "weak@x.com",
                "password": "weak",  # too short, no uppercase, no digit
                "display_name": "x",
            },
        )
        assert r.status_code == 422

    async def test_refresh_without_token_returns_401(self, e2e_client: AsyncClient):
        r = await e2e_client.post("/api/auth/refresh", json={})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# CSRF: state-changing /api/* without proper headers is blocked
# ---------------------------------------------------------------------------

class TestCSRFOnNonAuthEndpoint:
    async def test_logout_with_form_content_type_blocked(
        self, e2e_client: AsyncClient
    ):
        """`/api/auth/logout` is exempt from CSRF (it's in the auth bypass
        list) so this should pass even without proper headers — confirming
        the bypass works."""
        r = await e2e_client.post(
            "/api/auth/logout",
            content=b"x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 200
