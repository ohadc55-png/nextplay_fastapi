"""Tests for clubs + invite_codes repos.

Most important: `mark_redeemed` must be safe against double-redeem races.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.clubs import Club, InviteCode
from src.models.users import User
from src.repositories.clubs_repo import ClubsRepository, InviteCodesRepository


class TestClubsRepository:
    async def test_create_then_get(self, db_session: AsyncSession):
        repo = ClubsRepository(db_session)
        club = Club(name="Acme")
        created = await repo.create(club)
        fetched = await repo.get(created.id)
        assert fetched is not None and fetched.name == "Acme"


class TestInviteCodesRepository:
    async def test_get_unredeemed_filters_redeemed(self, db_session: AsyncSession):
        repo = InviteCodesRepository(db_session)
        u = User(email="r@x.com", display_name="R")
        db_session.add(u)
        await db_session.flush()

        ic_open = InviteCode(code="OPEN-2025")
        ic_used = InviteCode(code="USED-2025", redeemed_by=u.id, redeemed_at="2026-01-01T00:00:00")
        db_session.add_all([ic_open, ic_used])
        await db_session.flush()

        assert (await repo.get_unredeemed_by_code("OPEN-2025")) is not None
        assert (await repo.get_unredeemed_by_code("USED-2025")) is None
        assert (await repo.get_unredeemed_by_code("MISSING")) is None

    async def test_mark_redeemed_first_wins(self, db_session: AsyncSession):
        """Two callers race to redeem the same code. Only the first should
        succeed — guarded UPDATE prevents the second from over-writing."""
        repo = InviteCodesRepository(db_session)
        u_a = User(email="a@x.com", display_name="A")
        u_b = User(email="b@x.com", display_name="B")
        db_session.add_all([u_a, u_b])
        await db_session.flush()
        ic = InviteCode(code="ONCE-2025")
        db_session.add(ic)
        await db_session.flush()

        first_won = await repo.mark_redeemed(code_id=ic.id, user_id=u_a.id)
        second_won = await repo.mark_redeemed(code_id=ic.id, user_id=u_b.id)
        assert first_won is True
        assert second_won is False

        # Confirm: the row is owned by user A, not B.
        assert (await repo.get_unredeemed_by_code("ONCE-2025")) is None  # consumed
        # Refresh from DB — the raw UPDATE bypassed the ORM identity map, so
        # the cached `ic` instance is stale.
        await db_session.refresh(ic)
        assert ic.redeemed_by == u_a.id

    async def test_count_redeemed_for_club(self, db_session: AsyncSession):
        repo = InviteCodesRepository(db_session)
        club = Club(name="Acme")
        db_session.add(club)
        await db_session.flush()
        u_1 = User(email="1@x.com", display_name="1")
        u_2 = User(email="2@x.com", display_name="2")
        db_session.add_all([u_1, u_2])
        await db_session.flush()

        # 1 used, 1 unused, both attached to club
        db_session.add(InviteCode(code="A", club_id=club.id))
        db_session.add(InviteCode(code="B", club_id=club.id, redeemed_by=u_1.id, redeemed_at="2026-01-01T00:00:00"))
        db_session.add(InviteCode(code="C", club_id=club.id, redeemed_by=u_2.id, redeemed_at="2026-01-02T00:00:00"))
        await db_session.flush()

        assert await repo.count_redeemed_for_club(club.id) == 2
