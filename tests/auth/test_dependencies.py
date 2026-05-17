"""Tests for auth dependencies.

Goal: prove the dependency wiring is correct end-to-end via a tiny
FastAPI app that mounts a few protected endpoints. Cookie + Bearer dual
transport, `require_active_subscription`, `require_pro`, club bypass —
all verified.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import src.models  # noqa: F401 — register all models
from src.api.deps.auth import (
    get_current_user,
    require_active_subscription,
    require_pro,
)
from src.auth.jwt_service import create_access_token
from src.core.config import settings
from src.core.database import Base, get_db
from src.core.exceptions import AppError
from src.models.clubs import Club
from src.models.users import User

JWT_SECRET = "test-secret-key-32-chars-long-xxxx"


# ---------------------------------------------------------------------------
# Per-test in-memory DB + a FastAPI test app that overrides get_db
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
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
async def app_factory(app_engine: AsyncEngine):
    """Returns (app, session_factory). The app has a few protected endpoints
    plus an override of get_db that yields sessions from `app_engine`."""
    factory = async_sessionmaker(app_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    async def _override_get_db():
        """Production-style: commit on success, rollback on exception. This
        is what lets out-of-band state-machine side effects (the trial→
        expired auto-flip's flush) persist after the request completes."""
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app = FastAPI()

    @app.get("/protected")
    async def _protected(user: User = Depends(get_current_user)):
        return {"id": user.id, "email": user.email}

    @app.get("/active-only")
    async def _active(user: User = Depends(require_active_subscription)):
        return {"id": user.id, "plan": user.subscription_plan, "club_id": user.club_id}

    @app.get("/pro-only")
    async def _pro(user: User = Depends(require_pro)):
        return {"id": user.id, "plan": user.subscription_plan}

    @app.exception_handler(AppError)
    async def _h(_r: Request, exc: AppError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, "code": exc.code})

    app.dependency_overrides[get_db] = _override_get_db
    return app, factory


@pytest_asyncio.fixture
async def client(app_factory) -> AsyncIterator[AsyncClient]:
    app, _ = app_factory
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def seed_user(app_factory):
    """Returns an async function that creates a user with given attrs."""
    _, factory = app_factory

    async def _make(**kwargs) -> User:
        async with factory() as s:
            u = User(
                email=kwargs.pop("email", "u@x.com"),
                display_name=kwargs.pop("display_name", "u"),
                **kwargs,
            )
            s.add(u)
            await s.commit()
            await s.refresh(u)
            return u

    return _make


# ---------------------------------------------------------------------------
# get_current_user — dual transport (cookie + Bearer)
# ---------------------------------------------------------------------------

class TestGetCurrentUser:
    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        r = await client.get("/protected")
        assert r.status_code == 401

    async def test_works_via_authorization_header(self, client: AsyncClient, seed_user):
        u = await seed_user(email="bearer@x.com")
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["email"] == "bearer@x.com"

    async def test_works_via_cookie(self, client: AsyncClient, seed_user):
        u = await seed_user(email="cookie@x.com")
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/protected", cookies={"access_token": token})
        assert r.status_code == 200
        assert r.json()["email"] == "cookie@x.com"

    async def test_authorization_takes_precedence_over_cookie(
        self, client: AsyncClient, seed_user
    ):
        """If both are present, Authorization header wins (matches v1.0-flask)."""
        u_header = await seed_user(email="header@x.com")
        u_cookie = await seed_user(email="cookie2@x.com")
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            header_tok = create_access_token(user_id=u_header.id, email=u_header.email)
            cookie_tok = create_access_token(user_id=u_cookie.id, email=u_cookie.email)
            r = await client.get(
                "/protected",
                headers={"Authorization": f"Bearer {header_tok}"},
                cookies={"access_token": cookie_tok},
            )
        assert r.status_code == 200
        assert r.json()["email"] == "header@x.com"

    async def test_invalid_token_returns_401(self, client: AsyncClient):
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            r = await client.get(
                "/protected", headers={"Authorization": "Bearer nope-not-a-jwt"}
            )
        assert r.status_code == 401

    async def test_inactive_user_returns_401(self, client: AsyncClient, seed_user):
        u = await seed_user(email="inactive@x.com", is_active=0)
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    async def test_soft_deleted_user_returns_401(self, client: AsyncClient, seed_user):
        u = await seed_user(email="deleted@x.com", deleted_at="2026-01-01T00:00:00")
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Subscription guards
# ---------------------------------------------------------------------------

class TestSubscriptionGuards:
    async def test_active_endpoint_blocks_expired_plan(self, client: AsyncClient, seed_user):
        u = await seed_user(email="expired@x.com", subscription_plan="expired")
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/active-only", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
        assert r.json()["code"] == "trial_expired"

    async def test_active_endpoint_allows_trial(self, client: AsyncClient, seed_user):
        # Trial users get access only while `trial_days_left > 0` — set a
        # `trial_ends_at` 14 days out so the gate at deps/auth.py sees a
        # real, in-window trial. A missing `trial_ends_at` is now treated
        # as already-expired (defensive: cost-gated endpoints shouldn't
        # fire OpenAI calls for a user with no trial deadline).
        future = (datetime.now(UTC) + timedelta(days=14)).isoformat()
        u = await seed_user(
            email="trial@x.com",
            subscription_plan="trial",
            trial_ends_at=future,
        )
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/active-only", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    async def test_pro_endpoint_blocks_basic(self, client: AsyncClient, seed_user):
        u = await seed_user(email="basic@x.com", subscription_plan="basic")
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/pro-only", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
        assert r.json()["code"] == "pro_required"

    async def test_pro_endpoint_allows_pro(self, client: AsyncClient, seed_user):
        u = await seed_user(email="pro@x.com", subscription_plan="pro")
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/pro-only", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    async def test_club_member_bypasses_subscription_guard(
        self, client: AsyncClient, seed_user, app_factory
    ):
        """**Regression**: club coaches must bypass `require_active_subscription`
        even if their `subscription_plan` is technically 'expired' or 'basic'."""
        _, factory = app_factory
        async with factory() as s:
            club = Club(name="Acme")
            s.add(club)
            await s.commit()
            await s.refresh(club)
            club_id = club.id

        u = await seed_user(
            email="club-coach@x.com",
            subscription_plan="expired",  # would normally hit 403
            club_id=club_id,  # …but club covers them
        )
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/active-only", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["club_id"] == club_id

    async def test_club_member_bypasses_pro_guard(
        self, client: AsyncClient, seed_user, app_factory
    ):
        _, factory = app_factory
        async with factory() as s:
            club = Club(name="Acme")
            s.add(club)
            await s.commit()
            await s.refresh(club)
            club_id = club.id

        u = await seed_user(
            email="club-basic@x.com",
            subscription_plan="basic",  # 'basic' would normally hit 403
            club_id=club_id,
        )
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/pro-only", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Trial-expired auto-flip side effect
# ---------------------------------------------------------------------------

class TestAutoFlipExpired:
    async def test_trial_past_user_is_flipped_on_first_request(
        self, client: AsyncClient, seed_user
    ):
        """A user whose trial ended yesterday must hit `/active-only` and
        get 403 — the in-memory mutation in `maybe_flip_expired` propagates
        through `require_active_subscription` immediately, so the cost-leak
        stop kicks in within the same request that detected the expiry.

        Persistence is achieved via a separate `AsyncSessionLocal()` in
        `flip_to_expired_and_schedule_purge` (matches v1's separate
        SQLite connection + conn.commit()), so the flip survives the
        request-scoped rollback that follows the 403."""
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        u = await seed_user(
            email="just-expired@x.com",
            subscription_plan="trial",
            trial_ends_at=past,
        )
        with patch.object(settings, "JWT_SECRET_KEY", JWT_SECRET):
            token = create_access_token(user_id=u.id, email=u.email)
            r = await client.get("/active-only", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
        assert r.json()["code"] == "trial_expired"
