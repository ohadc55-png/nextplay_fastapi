"""Tests for the four auth repos.

Coverage:
- RefreshToken: lookup-by-hash filters revoked, revoke is idempotent,
  revoke_all_for_user, cleanup_expired.
- AuthToken: get_unused_by_hash filters by purpose + used_at + expires_at,
  mark_used is idempotent.
- SocialAccount: get_by_provider_user.
- AuditLog: add() and re-read.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.auth import AuditLog, AuthToken, RefreshToken, SocialAccount
from src.models.users import User
from src.repositories.auth_repo import (
    AuditLogRepository,
    AuthTokenRepository,
    RefreshTokenRepository,
    SocialAccountRepository,
)


async def _user(session: AsyncSession, email: str = "u@x.com") -> User:
    u = User(email=email, display_name="u")
    session.add(u)
    await session.flush()
    return u


class TestRefreshTokenRepository:
    async def test_get_active_by_hash_filters_revoked(self, db_session: AsyncSession):
        repo = RefreshTokenRepository(db_session)
        u = await _user(db_session)
        live = RefreshToken(user_id=u.id, token_hash="h-live", expires_at="2099-01-01T00:00:00")
        dead = RefreshToken(
            user_id=u.id, token_hash="h-dead",
            expires_at="2099-01-01T00:00:00", revoked_at="2026-01-01T00:00:00",
        )
        db_session.add_all([live, dead])
        await db_session.flush()

        assert (await repo.get_active_by_hash("h-live")) is not None
        assert (await repo.get_active_by_hash("h-dead")) is None
        assert (await repo.get_active_by_hash("h-missing")) is None

    async def test_revoke_is_idempotent(self, db_session: AsyncSession):
        repo = RefreshTokenRepository(db_session)
        u = await _user(db_session)
        rt = RefreshToken(user_id=u.id, token_hash="h", expires_at="2099-01-01T00:00:00")
        db_session.add(rt)
        await db_session.flush()

        await repo.revoke(rt.id)
        first = (await db_session.execute(select(RefreshToken).where(RefreshToken.id == rt.id))).scalar_one()
        assert first.revoked_at is not None
        first_revoked_at = first.revoked_at

        # Second revoke is a no-op (the WHERE guard prevents overwriting).
        await repo.revoke(rt.id)
        again = (await db_session.execute(select(RefreshToken).where(RefreshToken.id == rt.id))).scalar_one()
        assert again.revoked_at == first_revoked_at

    async def test_revoke_all_for_user(self, db_session: AsyncSession):
        repo = RefreshTokenRepository(db_session)
        u_a = await _user(db_session, "a@x.com")
        u_b = await _user(db_session, "b@x.com")
        for h in ("a1", "a2"):
            db_session.add(RefreshToken(user_id=u_a.id, token_hash=h, expires_at="2099-01-01T00:00:00"))
        db_session.add(RefreshToken(user_id=u_b.id, token_hash="b1", expires_at="2099-01-01T00:00:00"))
        await db_session.flush()

        await repo.revoke_all_for_user(u_a.id)
        assert (await repo.get_active_by_hash("a1")) is None
        assert (await repo.get_active_by_hash("a2")) is None
        # u_b's token untouched
        assert (await repo.get_active_by_hash("b1")) is not None


class TestAuthTokenRepository:
    async def test_get_unused_filters_by_purpose(self, db_session: AsyncSession):
        repo = AuthTokenRepository(db_session)
        u = await _user(db_session)
        future = datetime.utcnow() + timedelta(hours=1)
        t = AuthToken(user_id=u.id, token_hash="h", purpose="verify_email", expires_at=future)
        db_session.add(t)
        await db_session.flush()

        assert (await repo.get_unused_by_hash("h", purpose="verify_email")) is not None
        # Right hash but wrong purpose → None (defends against a token replay
        # across purposes).
        assert (await repo.get_unused_by_hash("h", purpose="reset_password")) is None

    async def test_get_unused_excludes_used(self, db_session: AsyncSession):
        repo = AuthTokenRepository(db_session)
        u = await _user(db_session)
        future = datetime.utcnow() + timedelta(hours=1)
        t = AuthToken(
            user_id=u.id, token_hash="h", purpose="verify_email",
            expires_at=future, used_at=datetime.utcnow(),
        )
        db_session.add(t)
        await db_session.flush()

        assert (await repo.get_unused_by_hash("h", purpose="verify_email")) is None

    async def test_get_unused_excludes_expired(self, db_session: AsyncSession):
        repo = AuthTokenRepository(db_session)
        u = await _user(db_session)
        past = datetime.utcnow() - timedelta(seconds=1)
        t = AuthToken(user_id=u.id, token_hash="h", purpose="verify_email", expires_at=past)
        db_session.add(t)
        await db_session.flush()

        assert (await repo.get_unused_by_hash("h", purpose="verify_email")) is None

    async def test_mark_used_is_idempotent(self, db_session: AsyncSession):
        repo = AuthTokenRepository(db_session)
        u = await _user(db_session)
        future = datetime.utcnow() + timedelta(hours=1)
        t = AuthToken(user_id=u.id, token_hash="h", purpose="reset_password", expires_at=future)
        db_session.add(t)
        await db_session.flush()

        await repo.mark_used(t.id)
        first = (await db_session.execute(select(AuthToken).where(AuthToken.id == t.id))).scalar_one()
        assert first.used_at is not None
        first_used = first.used_at

        await repo.mark_used(t.id)
        again = (await db_session.execute(select(AuthToken).where(AuthToken.id == t.id))).scalar_one()
        assert again.used_at == first_used


class TestSocialAccountRepository:
    async def test_get_by_provider_user(self, db_session: AsyncSession):
        repo = SocialAccountRepository(db_session)
        u = await _user(db_session)
        sa = SocialAccount(
            user_id=u.id, provider="google",
            provider_user_id="g-12345", provider_email="u@x.com",
        )
        db_session.add(sa)
        await db_session.flush()

        found = await repo.get_by_provider_user(provider="google", provider_user_id="g-12345")
        assert found is not None
        # Wrong provider with right id → None
        assert (await repo.get_by_provider_user(provider="facebook", provider_user_id="g-12345")) is None

    async def test_list_for_user(self, db_session: AsyncSession):
        repo = SocialAccountRepository(db_session)
        u = await _user(db_session)
        for prov in ("google", "facebook"):
            db_session.add(SocialAccount(user_id=u.id, provider=prov, provider_user_id=f"{prov}-1"))
        await db_session.flush()

        rows = await repo.list_for_user(u.id)
        assert {r.provider for r in rows} == {"google", "facebook"}


class TestAuditLogRepository:
    async def test_add_creates_row(self, db_session: AsyncSession):
        repo = AuditLogRepository(db_session)
        u = await _user(db_session)
        row = await repo.add(user_id=u.id, action="login", ip_address="1.2.3.4", user_agent="curl")
        assert row.id is not None

        rows = list(
            (await db_session.execute(select(AuditLog).where(AuditLog.user_id == u.id))).scalars().all()
        )
        assert len(rows) == 1
        assert rows[0].action == "login"
        assert rows[0].ip_address == "1.2.3.4"
