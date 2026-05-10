"""Tests for trial/purge state machine.

Critical regression tests:
- `should_purge` returns False for club members (club_id IS NOT NULL)
- `should_purge` returns False before grace period elapses
- `purge_user_data` deletes team-owned content but keeps the users row
- `maybe_flip_expired` flips trial-past users to 'expired' + schedules purge
- `maybe_flip_expired` does NOT flip club members
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.purge_service import (
    maybe_flip_expired,
    purge_user_data,
    should_purge,
)
from src.models.clubs import Club
from src.models.conversations import Conversation
from src.models.players import Player
from src.models.teams import TeamProfile
from src.models.users import User


async def _user(session: AsyncSession, email: str, **kwargs) -> User:
    u = User(email=email, display_name="u", **kwargs)
    session.add(u)
    await session.flush()
    return u


async def _seed_team_with_data(session: AsyncSession, owner: User) -> TeamProfile:
    """Create a team + a player + a conversation row so we can verify purge."""
    team = TeamProfile(team_name="Eagles", user_id=owner.id)
    session.add(team)
    await session.flush()
    session.add(Player(name="LeBron", user_id=owner.id, team_id=team.id))
    session.add(Conversation(
        session_id="s1", user_id=owner.id, team_id=team.id,
        role="user", content="hi",
    ))
    await session.flush()
    return team


# ---------------------------------------------------------------------------
# should_purge — defensive gate
# ---------------------------------------------------------------------------

class TestShouldPurge:
    async def test_club_member_is_exempt(self, db_session: AsyncSession):
        """**Regression-critical**: a club member must never be purged
        regardless of subscription_plan or data_purge_at."""
        club = Club(name="Sponsoring Club")
        db_session.add(club)
        await db_session.flush()
        u = await _user(
            db_session, "club@x.com",
            club_id=club.id,
            subscription_plan="expired",
            data_purge_at=datetime.now(UTC) - timedelta(days=1),  # past!
        )
        assert should_purge(u) is False

    async def test_active_trial_is_not_purged(self, db_session: AsyncSession):
        u = await _user(db_session, "active@x.com", subscription_plan="trial")
        assert should_purge(u) is False

    async def test_expired_within_grace_is_not_purged(self, db_session: AsyncSession):
        future = datetime.now(UTC) + timedelta(days=10)
        u = await _user(
            db_session, "grace@x.com",
            subscription_plan="expired",
            data_purge_at=future,
        )
        assert should_purge(u) is False

    async def test_expired_past_grace_is_purged(self, db_session: AsyncSession):
        past = datetime.now(UTC) - timedelta(days=1)
        u = await _user(
            db_session, "purge-me@x.com",
            subscription_plan="expired",
            data_purge_at=past,
        )
        assert should_purge(u) is True

    async def test_no_data_purge_at_is_not_purged(self, db_session: AsyncSession):
        u = await _user(db_session, "nopurge@x.com", subscription_plan="expired")
        assert should_purge(u) is False


# ---------------------------------------------------------------------------
# purge_user_data — deletion
# ---------------------------------------------------------------------------

class TestPurgeUserData:
    async def test_deletes_team_owned_content(self, db_session: AsyncSession):
        u = await _user(db_session, "victim@x.com", subscription_plan="expired")
        await _seed_team_with_data(db_session, u)

        result = await purge_user_data(db_session, u.id)

        assert result["total"] > 0
        # Spot-check: a few key tables had rows deleted
        assert result["deleted"]["players"] >= 1
        assert result["deleted"]["team_profile"] >= 1
        assert result["deleted"]["conversations"] >= 1

    async def test_users_row_is_kept(self, db_session: AsyncSession):
        """Account row must survive — coach can re-login + upgrade."""
        u = await _user(db_session, "kept@x.com", subscription_plan="expired")
        await _seed_team_with_data(db_session, u)

        await purge_user_data(db_session, u.id)

        still_exists = (
            await db_session.execute(select(User).where(User.id == u.id))
        ).scalar_one_or_none()
        assert still_exists is not None
        assert still_exists.email == "kept@x.com"

    async def test_clears_data_purge_at(self, db_session: AsyncSession):
        """After purging, `data_purge_at` is NULL so the gate doesn't re-fire
        on the next request."""
        u = await _user(
            db_session, "clear@x.com",
            subscription_plan="expired",
            data_purge_at=datetime.now(UTC) - timedelta(days=1),
        )
        await purge_user_data(db_session, u.id)

        await db_session.refresh(u)
        assert u.data_purge_at is None

    async def test_other_users_data_is_not_touched(self, db_session: AsyncSession):
        """Cross-tenant safety: purging coach A doesn't touch coach B's rows."""
        u_a = await _user(db_session, "victim@x.com", subscription_plan="expired")
        u_b = await _user(db_session, "spectator@x.com")
        await _seed_team_with_data(db_session, u_a)
        team_b = await _seed_team_with_data(db_session, u_b)

        await purge_user_data(db_session, u_a.id)

        # u_b's team + player + conversation still there
        b_players = (
            await db_session.execute(select(Player).where(Player.user_id == u_b.id))
        ).scalars().all()
        b_team = await db_session.get(TeamProfile, team_b.id)
        b_convs = (
            await db_session.execute(select(Conversation).where(Conversation.user_id == u_b.id))
        ).scalars().all()
        assert len(list(b_players)) == 1
        assert b_team is not None
        assert len(list(b_convs)) == 1

    async def test_empty_user_id_is_noop(self, db_session: AsyncSession):
        result = await purge_user_data(db_session, 0)
        assert result == {"deleted": {}, "failed": [], "total": 0}


# ---------------------------------------------------------------------------
# maybe_flip_expired — trial → expired transition
# ---------------------------------------------------------------------------

class TestMaybeFlipExpired:
    async def test_flips_trial_past_user(self, db_session: AsyncSession):
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        u = await _user(
            db_session, "trial-done@x.com",
            subscription_plan="trial",
            trial_ends_at=past,
        )

        flipped = await maybe_flip_expired(db_session, u)

        assert flipped is True
        await db_session.refresh(u)
        assert u.subscription_plan == "expired"
        assert u.data_purge_at is not None  # purge scheduled

    async def test_does_not_flip_active_trial(self, db_session: AsyncSession):
        future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        u = await _user(
            db_session, "still-trial@x.com",
            subscription_plan="trial",
            trial_ends_at=future,
        )

        flipped = await maybe_flip_expired(db_session, u)

        assert flipped is False
        assert u.subscription_plan == "trial"

    async def test_does_not_flip_club_member(self, db_session: AsyncSession):
        """**Regression**: a trial-past coach who's a club member must NOT
        be flipped to expired. Club covers them."""
        club = Club(name="Acme")
        db_session.add(club)
        await db_session.flush()
        past = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        u = await _user(
            db_session, "club-trial@x.com",
            club_id=club.id,
            subscription_plan="trial",
            trial_ends_at=past,
        )

        flipped = await maybe_flip_expired(db_session, u)

        assert flipped is False
        assert u.subscription_plan == "trial"

    async def test_does_not_flip_already_expired(self, db_session: AsyncSession):
        u = await _user(db_session, "already-done@x.com", subscription_plan="expired")
        flipped = await maybe_flip_expired(db_session, u)
        assert flipped is False
