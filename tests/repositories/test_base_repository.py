"""Smoke tests for BaseRepository + TeamScopedRepository.

Uses the `User` and `Player` models as concrete subjects:
- User: simple PK lookup, no tenant scoping (tests BaseRepository basics).
- Player: tenant-scoped (user_id + team_id) (tests the multi-tenancy gate).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.players import Player
from src.models.teams import TeamProfile
from src.models.users import User
from src.repositories.base_repository import BaseRepository, TeamScopedRepository


# ---------------------------------------------------------------------------
# Concrete repos used only in this test file
# ---------------------------------------------------------------------------

class _UsersRepo(BaseRepository[User]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, User)


class _PlayersRepo(TeamScopedRepository[Player]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Player)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_user(session: AsyncSession, email: str) -> User:
    u = User(email=email, display_name=email.split("@")[0])
    session.add(u)
    await session.flush()
    return u


async def _make_team(session: AsyncSession, owner: User, name: str) -> TeamProfile:
    t = TeamProfile(team_name=name, user_id=owner.id)
    session.add(t)
    await session.flush()
    return t


async def _make_player(session: AsyncSession, name: str, *, user_id: int, team_id: int) -> Player:
    p = Player(name=name, user_id=user_id, team_id=team_id)
    session.add(p)
    await session.flush()
    return p


# ---------------------------------------------------------------------------
# BaseRepository
# ---------------------------------------------------------------------------

class TestBaseRepository:
    async def test_create_then_get(self, db_session: AsyncSession):
        repo = _UsersRepo(db_session)
        user = User(email="alice@example.com", display_name="Alice")
        created = await repo.create(user)
        assert created.id is not None

        fetched = await repo.get(created.id)
        assert fetched is not None
        assert fetched.email == "alice@example.com"

    async def test_get_returns_none_for_missing_id(self, db_session: AsyncSession):
        repo = _UsersRepo(db_session)
        assert await repo.get(99999) is None

    async def test_update_persists_changes(self, db_session: AsyncSession):
        repo = _UsersRepo(db_session)
        user = await _make_user(db_session, "bob@example.com")
        user.display_name = "Robert"
        await repo.update(user)

        fetched = await repo.get(user.id)
        assert fetched is not None
        assert fetched.display_name == "Robert"

    async def test_delete_removes_row(self, db_session: AsyncSession):
        repo = _UsersRepo(db_session)
        user = await _make_user(db_session, "to-delete@example.com")
        await repo.delete(user)
        assert await repo.get(user.id) is None

    async def test_delete_by_id_returns_true_when_hit(self, db_session: AsyncSession):
        repo = _UsersRepo(db_session)
        user = await _make_user(db_session, "deleteme@example.com")
        assert await repo.delete_by_id(user.id) is True
        assert await repo.delete_by_id(user.id) is False  # second call: nothing to delete

    async def test_list_all(self, db_session: AsyncSession):
        repo = _UsersRepo(db_session)
        await _make_user(db_session, "a@x.com")
        await _make_user(db_session, "b@x.com")
        rows = await repo.list_all()
        assert {u.email for u in rows} == {"a@x.com", "b@x.com"}


# ---------------------------------------------------------------------------
# TeamScopedRepository — the multi-tenancy gate
# ---------------------------------------------------------------------------

class TestTeamScopedRepository:
    async def test_list_for_user_team_returns_empty_when_both_args_missing(
        self, db_session: AsyncSession
    ):
        """The defensive at-least-one-of gate: passing (None, None) MUST
        return [] without scanning the table — this is what prevents an
        unscoped query from leaking cross-tenant data."""
        repo = _PlayersRepo(db_session)
        # Seed a player so the table is non-empty; gate must still return [].
        u = await _make_user(db_session, "coach@x.com")
        team = await _make_team(db_session, u, "Eagles")
        await _make_player(db_session, "P1", user_id=u.id, team_id=team.id)

        assert await repo.list_for_user_team(None, None) == []

    async def test_list_for_user_team_filters_by_user_only(self, db_session: AsyncSession):
        repo = _PlayersRepo(db_session)
        u_a = await _make_user(db_session, "a@x.com")
        u_b = await _make_user(db_session, "b@x.com")
        team_a = await _make_team(db_session, u_a, "A's team")
        team_b = await _make_team(db_session, u_b, "B's team")
        await _make_player(db_session, "Alice's player", user_id=u_a.id, team_id=team_a.id)
        await _make_player(db_session, "Bob's player", user_id=u_b.id, team_id=team_b.id)

        rows = await repo.list_for_user_team(user_id=u_a.id, team_id=None)
        assert {r.name for r in rows} == {"Alice's player"}

    async def test_cross_tenant_isolation(self, db_session: AsyncSession):
        """Two users sharing the same `team_id` (impossible by design but worth
        proving) — passing user_id + team_id filters by BOTH so the right user
        only sees their own row."""
        repo = _PlayersRepo(db_session)
        u_a = await _make_user(db_session, "a@x.com")
        u_b = await _make_user(db_session, "b@x.com")
        team = await _make_team(db_session, u_a, "shared")
        await _make_player(db_session, "A's player", user_id=u_a.id, team_id=team.id)
        await _make_player(db_session, "B's player on A's team", user_id=u_b.id, team_id=team.id)

        rows_a = await repo.list_for_user_team(user_id=u_a.id, team_id=team.id)
        rows_b = await repo.list_for_user_team(user_id=u_b.id, team_id=team.id)
        assert {r.name for r in rows_a} == {"A's player"}
        assert {r.name for r in rows_b} == {"B's player on A's team"}

    async def test_get_for_user_team_returns_none_on_tenant_mismatch(
        self, db_session: AsyncSession
    ):
        """Even if the row exists, get_for_user_team must return None when
        the (user_id, team_id) scope doesn't match. This is the right primitive
        for endpoints that load 'get this entity if it belongs to me'."""
        repo = _PlayersRepo(db_session)
        u_a = await _make_user(db_session, "owner@x.com")
        u_b = await _make_user(db_session, "attacker@x.com")
        team_a = await _make_team(db_session, u_a, "owner's team")
        player = await _make_player(db_session, "owned", user_id=u_a.id, team_id=team_a.id)

        # Owner can fetch
        assert await repo.get_for_user_team(player.id, u_a.id, team_a.id) is not None
        # Attacker (wrong user) cannot
        assert await repo.get_for_user_team(player.id, u_b.id, team_a.id) is None

    async def test_get_for_user_team_returns_none_when_no_scope(self, db_session: AsyncSession):
        repo = _PlayersRepo(db_session)
        u = await _make_user(db_session, "u@x.com")
        team = await _make_team(db_session, u, "team")
        p = await _make_player(db_session, "x", user_id=u.id, team_id=team.id)
        assert await repo.get_for_user_team(p.id, None, None) is None
