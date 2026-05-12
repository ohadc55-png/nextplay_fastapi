"""Tests for /org/api/players/* (Phase 1.6) — player CRUD only.

PlayerContact (encrypted) tests live in test_org_player_contacts.py.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_team_for_admin(
    org_admin_client: AsyncClient, api_session_factory, branch_id=None,
) -> int:
    from src.models.teams import TeamProfile
    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        t = TeamProfile(
            organization_id=org_id, branch_id=branch_id, team_name="Test Squad",
        )
        s.add(t)
        await s.commit()
        return t.id


async def _seed_player(api_session_factory, *, org_id, team_id, name="P", number=1):
    from src.models.players import Player
    async with api_session_factory() as s:
        p = Player(
            organization_id=org_id, team_id=team_id,
            name=name, number=number, active=True,
        )
        s.add(p)
        await s.commit()
        return p.id


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_list_empty(org_admin_client: AsyncClient):
    r = await org_admin_client.get("/org/api/players")
    assert r.status_code == 200
    assert r.json() == {"players": []}


async def test_list_excludes_private_coach_players(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Players with organization_id IS NULL must NEVER appear in /org/api/players."""
    from src.models.players import Player

    org_id = org_admin_client.org_seed["organization_id"]
    team_id = await _seed_team_for_admin(org_admin_client, api_session_factory)
    await _seed_player(api_session_factory, org_id=org_id, team_id=team_id, name="Org Player")

    # Private-coach player — no org. Must not leak.
    async with api_session_factory() as s:
        s.add(Player(organization_id=None, name="Private Player"))
        await s.commit()

    r = await org_admin_client.get("/org/api/players")
    names = {p["name"] for p in r.json()["players"]}
    assert "Org Player" in names
    assert "Private Player" not in names


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_player(
    org_admin_client: AsyncClient, api_session_factory,
):
    team_id = await _seed_team_for_admin(org_admin_client, api_session_factory)
    r = await org_admin_client.post(
        "/org/api/players",
        json={
            "name": "Yossi Lev",
            "team_id": team_id,
            "number": 7,
            "position": "PG",
            "age": 14,
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Yossi Lev"
    assert body["team_id"] == team_id
    assert body["number"] == 7
    assert body["active"] is True


async def test_create_with_cross_org_team_returns_404(
    org_admin_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from src.models.teams import TeamProfile

    other = await seed_org_admin(email="o@x.test", org_slug="o-x", org_name="O")
    async with api_session_factory() as s:
        foreign = TeamProfile(organization_id=other["organization_id"], team_name="Foreign")
        s.add(foreign)
        await s.commit()
        foreign_id = foreign.id

    r = await org_admin_client.post(
        "/org/api/players", json={"name": "X", "team_id": foreign_id},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Get / Patch / Delete
# ---------------------------------------------------------------------------


async def test_get_cross_tenant_returns_404(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from src.models.teams import TeamProfile

    a = await seed_org_admin(email="aa@x.test", org_slug="aa-x", org_name="AA")
    b = await seed_org_admin(email="bb@x.test", org_slug="bb-x", org_name="BB")
    async with api_session_factory() as s:
        team = TeamProfile(organization_id=a["organization_id"], team_name="A team")
        s.add(team)
        await s.flush()
        tid = team.id
    pid = await _seed_player(
        api_session_factory, org_id=a["organization_id"], team_id=tid, name="Hidden",
    )

    await api_client.post(
        "/org/login", json={"email": b["email"], "password": b["password"]}
    )
    r = await api_client.get(f"/org/api/players/{pid}")
    assert r.status_code == 404


async def test_patch_player(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    tid = await _seed_team_for_admin(org_admin_client, api_session_factory)
    pid = await _seed_player(api_session_factory, org_id=org_id, team_id=tid, name="Old")
    r = await org_admin_client.patch(
        f"/org/api/players/{pid}", json={"name": "New", "number": 99},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "New"
    assert body["number"] == 99


async def test_soft_delete_sets_active_false(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.players import Player
    org_id = org_admin_client.org_seed["organization_id"]
    tid = await _seed_team_for_admin(org_admin_client, api_session_factory)
    pid = await _seed_player(api_session_factory, org_id=org_id, team_id=tid)

    r = await org_admin_client.delete(f"/org/api/players/{pid}")
    assert r.status_code == 200

    async with api_session_factory() as s:
        p = await s.get(Player, pid)
        assert p is not None  # row preserved
        assert p.active is False


async def test_list_excludes_inactive_by_default(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    tid = await _seed_team_for_admin(org_admin_client, api_session_factory)
    active_id = await _seed_player(api_session_factory, org_id=org_id, team_id=tid, name="Active")
    inactive_id = await _seed_player(api_session_factory, org_id=org_id, team_id=tid, name="Inactive")
    await org_admin_client.delete(f"/org/api/players/{inactive_id}")

    r = await org_admin_client.get("/org/api/players")
    names = {p["name"] for p in r.json()["players"]}
    assert "Active" in names
    assert "Inactive" not in names

    r2 = await org_admin_client.get("/org/api/players?include_inactive=true")
    names2 = {p["name"] for p in r2.json()["players"]}
    assert "Inactive" in names2


# ---------------------------------------------------------------------------
# Role scoping
# ---------------------------------------------------------------------------


async def test_coach_sees_only_own_players(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """A `coach`'s GET /org/api/players returns only players whose team
    has team.user_id == coach.user_id."""
    from src.models.teams import TeamProfile

    creds = await seed_org_admin(email="cc@x.test", role="coach")
    org_id = creds["organization_id"]

    async with api_session_factory() as s:
        my_team = TeamProfile(
            organization_id=org_id, team_name="Mine", user_id=creds["user_id"],
        )
        other_team = TeamProfile(
            organization_id=org_id, team_name="Other", user_id=None,
        )
        s.add_all([my_team, other_team])
        await s.commit()
        my_tid, other_tid = my_team.id, other_team.id

    await _seed_player(api_session_factory, org_id=org_id, team_id=my_tid, name="Mine P")
    await _seed_player(api_session_factory, org_id=org_id, team_id=other_tid, name="Other P")

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/players")
    names = {p["name"] for p in r.json()["players"]}
    assert "Mine P" in names
    assert "Other P" not in names


# ---------------------------------------------------------------------------
# Phase 2.5 — player deliveries visibility
# ---------------------------------------------------------------------------


async def test_player_deliveries_endpoint_returns_empty_when_none(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    tid = await _seed_team_for_admin(org_admin_client, api_session_factory)
    pid = await _seed_player(
        api_session_factory, org_id=org_id, team_id=tid, name="No-Deliv",
    )
    r = await org_admin_client.get(f"/org/api/players/{pid}/deliveries")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["player"]["id"] == pid
    assert body["deliveries"] == []


async def test_players_list_includes_pending_approvals_field(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    tid = await _seed_team_for_admin(org_admin_client, api_session_factory)
    await _seed_player(api_session_factory, org_id=org_id, team_id=tid, name="P1")

    r = await org_admin_client.get("/org/api/players")
    assert r.status_code == 200
    rows = r.json()["players"]
    assert rows
    assert "pending_approvals" in rows[0]
    assert rows[0]["pending_approvals"] == 0
