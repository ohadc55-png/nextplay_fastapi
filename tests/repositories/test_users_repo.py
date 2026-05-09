"""Tests for UsersRepository.

Focus on the custom lookup + targeted-update methods. CRUD is covered by
BaseRepository tests.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.clubs import Club
from src.models.users import User
from src.repositories.users_repo import UsersRepository


async def _seed_user(session: AsyncSession, email: str, **kwargs) -> User:
    u = User(email=email, display_name=email.split("@")[0], **kwargs)
    session.add(u)
    await session.flush()
    return u


class TestUsersRepository:
    async def test_get_active_excludes_soft_deleted(self, db_session: AsyncSession):
        repo = UsersRepository(db_session)
        u_live = await _seed_user(db_session, "live@x.com")
        u_dead = await _seed_user(db_session, "dead@x.com", deleted_at="2026-01-01T00:00:00")

        assert (await repo.get_active(u_live.id)) is not None
        assert (await repo.get_active(u_dead.id)) is None

    async def test_get_by_email_active(self, db_session: AsyncSession):
        repo = UsersRepository(db_session)
        await _seed_user(db_session, "c1@x.com")
        await _seed_user(db_session, "deleted@x.com", deleted_at="2026-01-01T00:00:00")

        assert (await repo.get_by_email_active("c1@x.com")) is not None
        assert (await repo.get_by_email_active("deleted@x.com")) is None
        assert (await repo.get_by_email_active("nope@x.com")) is None

    async def test_soft_delete_sets_deleted_at_and_inactive(self, db_session: AsyncSession):
        repo = UsersRepository(db_session)
        u = await _seed_user(db_session, "byebye@x.com")

        await repo.soft_delete(u.id)
        # Re-read raw row (the cached instance might be stale)
        fresh = (await db_session.execute(select(User).where(User.id == u.id))).scalar_one()
        assert fresh.deleted_at is not None
        assert fresh.is_active == 0
        # And the active-only lookup hides it
        assert (await repo.get_active(u.id)) is None

    async def test_mark_logged_in_sets_last_login_at(self, db_session: AsyncSession):
        repo = UsersRepository(db_session)
        u = await _seed_user(db_session, "login@x.com")
        assert u.last_login_at is None

        await repo.mark_logged_in(u.id)
        fresh = (await db_session.execute(select(User).where(User.id == u.id))).scalar_one()
        assert fresh.last_login_at is not None

    async def test_unsubscribe_token_lookup(self, db_session: AsyncSession):
        repo = UsersRepository(db_session)
        await _seed_user(db_session, "u1@x.com", unsubscribe_token="abc-123")
        await _seed_user(db_session, "u2@x.com")

        found = await repo.get_by_unsubscribe_token("abc-123")
        assert found is not None and found.email == "u1@x.com"
        assert (await repo.get_by_unsubscribe_token("nope")) is None

    async def test_club_attach_detach_and_count(self, db_session: AsyncSession):
        repo = UsersRepository(db_session)
        # Seed a club + 2 users not yet attached
        club = Club(name="Eagles Academy")
        db_session.add(club)
        await db_session.flush()
        u1 = await _seed_user(db_session, "c1@x.com")
        u2 = await _seed_user(db_session, "c2@x.com")

        assert await repo.count_in_club(club.id) == 0

        await repo.attach_to_club(u1.id, club.id)
        await repo.attach_to_club(u2.id, club.id)
        assert await repo.count_in_club(club.id) == 2

        await repo.detach_from_club(u1.id)
        assert await repo.count_in_club(club.id) == 1

    async def test_set_subscription_clears_trial_ends_at(self, db_session: AsyncSession):
        repo = UsersRepository(db_session)
        u = await _seed_user(db_session, "trial@x.com", trial_ends_at="2026-12-31T00:00:00")

        await repo.set_subscription(u.id, plan="pro", trial_ends_at=None)
        fresh = (await db_session.execute(select(User).where(User.id == u.id))).scalar_one()
        assert fresh.subscription_plan == "pro"
        assert fresh.trial_ends_at is None

    async def test_mark_email_verified(self, db_session: AsyncSession):
        repo = UsersRepository(db_session)
        u = await _seed_user(db_session, "ver@x.com")
        assert u.email_verified == 0

        await repo.mark_email_verified(u.id)
        fresh = (await db_session.execute(select(User).where(User.id == u.id))).scalar_one()
        assert fresh.email_verified == 1
