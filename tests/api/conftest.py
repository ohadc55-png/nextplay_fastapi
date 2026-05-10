"""Shared fixtures for API endpoint tests.

Each test module gets a fresh in-memory SQLite database and a logged-in
AsyncClient with cookies persisted across calls. The factory is exposed
as `auth_client` so individual tests can spin up multiple users when
they need to test cross-tenant isolation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
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
async def api_engine() -> AsyncIterator[AsyncEngine]:
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
async def api_session_factory(api_engine: AsyncEngine):
    """Lets a test seed rows directly without going through an HTTP call."""
    return async_sessionmaker(
        api_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest_asyncio.fixture
async def api_client(api_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """Anonymous client. Use `register` / `login` from your test, or use
    `authed_client` for a pre-authenticated one."""
    factory = async_sessionmaker(
        api_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    try:
        # Rate limiter is OFF by default in tests — every test re-uses
        # a single app instance and would otherwise blow past the cap.
        # The dedicated rate-limit tests opt back in via patch.object.
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET), \
             patch.object(settings, "RATE_LIMIT_ENABLED", False):
            # X-Requested-With satisfies the CSRF middleware on every
            # state-changing /api/* request — matches what the SPA's
            # fetch wrapper sets in real life.
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"X-Requested-With": "XMLHttpRequest"},
            ) as c:
                yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def authed_client(api_client: AsyncClient) -> AsyncClient:
    """Registers a fresh user and returns the same `api_client` with auth
    cookies set. Tests that need a known user_id can read it from the
    register response (`/api/auth/me` echoes id)."""
    r = await api_client.post(
        "/api/auth/register",
        json={
            "email": "tester@example.com",
            "password": "Sup3rSecure!",
            "display_name": "Tester",
        },
    )
    assert r.status_code == 200, r.text
    return api_client


@pytest_asyncio.fixture
async def register_user(
    api_client: AsyncClient,
) -> Callable[..., Awaitable[dict]]:
    """Lets a test create additional users on top of the same client."""

    async def _make(email: str, password: str = "Sup3rSecure!") -> dict:
        r = await api_client.post(
            "/api/auth/register",
            json={"email": email, "password": password, "display_name": email.split("@")[0]},
        )
        assert r.status_code == 200, r.text
        return r.json()

    return _make


# ---------------------------------------------------------------------------
# Org Admin (Phase 0) fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seed_org_admin(api_session_factory) -> Callable[..., Awaitable[dict]]:
    """Factory: seeds (User + Organization + UserOrganization) directly via the
    session, returning a dict with ids + the raw password (for login tests)."""
    from src.auth.password_service import hash_password
    from src.models.organizations import Organization
    from src.models.user_organizations import UserOrganization
    from src.models.users import User

    async def _make(
        *,
        email: str = "owner@org.test",
        password: str = "Sup3rSecure!",
        org_slug: str = "shaar-shivyon",
        org_name: str = "Sha'ar Shivyon",
        role: str = "org_admin",
    ) -> dict:
        async with api_session_factory() as s:
            user = User(
                email=email.lower(),
                password_hash=hash_password(password),
                display_name=email.split("@")[0],
            )
            s.add(user)
            await s.flush()

            org = Organization(slug=org_slug, name=org_name)
            s.add(org)
            await s.flush()

            membership = UserOrganization(
                user_id=user.id, organization_id=org.id, role=role,
            )
            s.add(membership)
            await s.commit()
            return {
                "user_id": user.id,
                "email": email.lower(),
                "password": password,
                "organization_id": org.id,
                "organization_slug": org_slug,
                "role": role,
            }

    return _make


@pytest_asyncio.fixture
async def org_admin_client(
    api_client: AsyncClient, seed_org_admin
) -> AsyncClient:
    """Logs an org_admin in via POST /org/login (JSON), returns same client
    with the org session cookie set."""
    creds = await seed_org_admin()
    r = await api_client.post(
        "/org/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    # Stash the seeded ids on the client for tests that need them.
    api_client.org_seed = creds  # type: ignore[attr-defined]
    return api_client
