"""Tests for /org/api/teams/* (Phase 1.5).

Critical invariant: private-coach teams (organization_id IS NULL) NEVER
appear in org-context responses. The corresponding Coach App endpoint
(/api/teams) keeps working for them. Both invariants get coverage.
"""

from __future__ import annotations

from sqlalchemy import select, update

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


async def _seed_branch(api_session_factory, org_id: int, name: str = "Branch") -> int:
    from src.models.branches import Branch
    async with api_session_factory() as s:
        b = Branch(organization_id=org_id, name=name)
        s.add(b)
        await s.commit()
        return b.id


async def _seed_branch_in_region(api_session_factory, org_id: int, region_id: int, name: str) -> int:
    from src.models.branches import Branch
    async with api_session_factory() as s:
        b = Branch(organization_id=org_id, region_id=region_id, name=name)
        s.add(b)
        await s.commit()
        return b.id


async def _seed_region(api_session_factory, org_id: int, name: str = "Region") -> int:
    from src.models.regions import Region
    async with api_session_factory() as s:
        r = Region(organization_id=org_id, name=name)
        s.add(r)
        await s.commit()
        return r.id


async def _seed_team(api_session_factory, org_id: int, *, branch_id: int | None = None,
                    name: str = "Team", user_id: int | None = None) -> int:
    from src.models.teams import TeamProfile
    async with api_session_factory() as s:
        t = TeamProfile(
            organization_id=org_id, branch_id=branch_id,
            team_name=name, user_id=user_id,
        )
        s.add(t)
        await s.commit()
        return t.id


async def _seed_coach_user(api_session_factory, org_id: int, *, email: str) -> dict:
    from src.auth.password_service import hash_password
    from src.models.user_organizations import UserOrganization
    from src.models.users import User
    async with api_session_factory() as s:
        u = User(
            email=email, password_hash=hash_password("Sup3rSecure!"),
            display_name=email.split("@")[0], email_verified=True,
        )
        s.add(u)
        await s.flush()
        uo = UserOrganization(
            user_id=u.id, organization_id=org_id, role="coach", status="active",
        )
        s.add(uo)
        await s.commit()
        return {"user_id": u.id, "membership_id": uo.id}


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_list_teams_empty(org_admin_client: AsyncClient):
    r = await org_admin_client.get("/org/api/teams")
    assert r.status_code == 200
    assert r.json() == {"teams": []}


async def test_list_teams_excludes_private_coach_teams(
    org_admin_client: AsyncClient, api_session_factory,
):
    """team_profile.organization_id IS NULL must never leak into /org/api/teams."""
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    branch_id = await _seed_branch(api_session_factory, org_id, name="B1")
    await _seed_team(api_session_factory, org_id, branch_id=branch_id, name="Org Team")

    # Private-coach team (no org). MUST NOT appear.
    async with api_session_factory() as s:
        s.add(TeamProfile(organization_id=None, team_name="Private Team"))
        await s.commit()

    r = await org_admin_client.get("/org/api/teams")
    assert r.status_code == 200
    teams = r.json()["teams"]
    names = {t["team_name"] for t in teams}
    assert "Org Team" in names
    assert "Private Team" not in names


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_team(org_admin_client: AsyncClient, api_session_factory):
    org_id = org_admin_client.org_seed["organization_id"]
    branch_id = await _seed_branch(api_session_factory, org_id)

    r = await org_admin_client.post(
        "/org/api/teams",
        json={"team_name": "Hapoel U16", "branch_id": branch_id, "division": "U16"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["team_name"] == "Hapoel U16"
    assert body["branch_id"] == branch_id
    assert body["organization_id"] == org_id
    assert body["division"] == "U16"


async def test_create_team_without_branch(org_admin_client: AsyncClient):
    r = await org_admin_client.post(
        "/org/api/teams", json={"team_name": "Floating Team"}
    )
    assert r.status_code == 201
    assert r.json()["branch_id"] is None


async def test_create_team_with_cross_org_branch_returns_404(
    org_admin_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """branch_id pointing at a branch in another org → 404."""
    other = await seed_org_admin(email="o@x.test", org_slug="o-x", org_name="O")
    foreign_branch_id = await _seed_branch(api_session_factory, other["organization_id"])

    r = await org_admin_client.post(
        "/org/api/teams",
        json={"team_name": "Bad", "branch_id": foreign_branch_id},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Get + Patch
# ---------------------------------------------------------------------------


async def test_get_team(org_admin_client: AsyncClient, api_session_factory):
    org_id = org_admin_client.org_seed["organization_id"]
    tid = await _seed_team(api_session_factory, org_id, name="Visible")
    r = await org_admin_client.get(f"/org/api/teams/{tid}")
    assert r.status_code == 200
    assert r.json()["team_name"] == "Visible"


async def test_get_team_cross_org_returns_404(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    a = await seed_org_admin(email="aa@x.test", org_slug="aa-x", org_name="AA")
    b = await seed_org_admin(email="bb@x.test", org_slug="bb-x", org_name="BB")
    tid_in_a = await _seed_team(api_session_factory, a["organization_id"], name="Hidden")

    await api_client.post(
        "/org/login", json={"email": b["email"], "password": b["password"]}
    )
    r = await api_client.get(f"/org/api/teams/{tid_in_a}")
    assert r.status_code == 404


async def test_patch_team_name(org_admin_client: AsyncClient, api_session_factory):
    org_id = org_admin_client.org_seed["organization_id"]
    tid = await _seed_team(api_session_factory, org_id, name="Old")
    r = await org_admin_client.patch(
        f"/org/api/teams/{tid}", json={"team_name": "New"}
    )
    assert r.status_code == 200
    assert r.json()["team_name"] == "New"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_team(org_admin_client: AsyncClient, api_session_factory):
    org_id = org_admin_client.org_seed["organization_id"]
    tid = await _seed_team(api_session_factory, org_id, name="Doomed")
    r = await org_admin_client.delete(f"/org/api/teams/{tid}")
    assert r.status_code == 200

    r2 = await org_admin_client.get(f"/org/api/teams/{tid}")
    assert r2.status_code == 404


async def test_delete_team_with_players_blocked(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.players import Player

    org_id = org_admin_client.org_seed["organization_id"]
    tid = await _seed_team(api_session_factory, org_id, name="WithPlayers")
    async with api_session_factory() as s:
        s.add(Player(team_id=tid, organization_id=org_id, name="P1"))
        await s.commit()

    r = await org_admin_client.delete(f"/org/api/teams/{tid}")
    assert r.status_code == 409
    assert r.json()["code"] == "team_has_players"


# ---------------------------------------------------------------------------
# Assign + unassign coach
# ---------------------------------------------------------------------------


async def test_assign_coach_idempotent(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    org_id = org_admin_client.org_seed["organization_id"]
    coach = await _seed_coach_user(api_session_factory, org_id, email="c@x.test")
    tid = await _seed_team(api_session_factory, org_id, name="Squad")

    r = await org_admin_client.post(
        f"/org/api/teams/{tid}/coaches", json={"user_id": coach["user_id"]}
    )
    assert r.status_code == 200
    assert r.json()["user_id"] == coach["user_id"]

    # Re-assign same coach: still 200, no duplicate membership row.
    r2 = await org_admin_client.post(
        f"/org/api/teams/{tid}/coaches", json={"user_id": coach["user_id"]}
    )
    assert r2.status_code == 200

    async with api_session_factory() as s:
        count = (await s.execute(
            select(UserOrganization).where(
                UserOrganization.user_id == coach["user_id"],
                UserOrganization.organization_id == org_id,
                UserOrganization.role == "coach",
            )
        )).all()
        assert len(count) == 1


async def test_assign_coach_creates_membership_for_new_coach(
    org_admin_client: AsyncClient, api_session_factory,
):
    """If the chosen user has no coach membership yet, assign_coach upserts one."""
    from src.auth.password_service import hash_password
    from src.models.user_organizations import UserOrganization
    from src.models.users import User

    org_id = org_admin_client.org_seed["organization_id"]
    tid = await _seed_team(api_session_factory, org_id, name="New Coach")
    async with api_session_factory() as s:
        u = User(
            email="brandnew@x.test", password_hash=hash_password("x"),
            display_name="Brand New", email_verified=True,
        )
        s.add(u)
        await s.commit()
        user_id = u.id

    r = await org_admin_client.post(
        f"/org/api/teams/{tid}/coaches", json={"user_id": user_id}
    )
    assert r.status_code == 200

    async with api_session_factory() as s:
        memberships = (await s.execute(
            select(UserOrganization).where(
                UserOrganization.user_id == user_id,
                UserOrganization.organization_id == org_id,
            )
        )).scalars().all()
        assert len(memberships) == 1
        assert memberships[0].role == "coach"


async def test_unassign_coach(org_admin_client: AsyncClient, api_session_factory):
    """DELETE clears team.user_id but leaves the user's coach membership row."""
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    org_id = org_admin_client.org_seed["organization_id"]
    coach = await _seed_coach_user(api_session_factory, org_id, email="cu@x.test")
    tid = await _seed_team(api_session_factory, org_id, name="WithCoach", user_id=coach["user_id"])

    r = await org_admin_client.delete(
        f"/org/api/teams/{tid}/coaches/{coach['user_id']}"
    )
    assert r.status_code == 200

    async with api_session_factory() as s:
        team = await s.get(TeamProfile, tid)
        assert team.user_id is None
        # Coach membership row still present.
        membership = await s.get(UserOrganization, coach["membership_id"])
        assert membership is not None
        assert membership.status == "active"


# ---------------------------------------------------------------------------
# Role gating — region_manager + branch_manager scoping
# ---------------------------------------------------------------------------


async def test_branch_manager_create_forced_to_own_branch(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """branch_manager omitting branch_id gets auto-pinned to their branch.
    Trying another branch returns 404."""
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="bm@x.test", role="branch_manager")
    org_id = creds["organization_id"]
    my_branch = await _seed_branch(api_session_factory, org_id, name="Mine")
    other_branch = await _seed_branch(api_session_factory, org_id, name="Other")
    async with api_session_factory() as s:
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "branch_manager",
            )
            .values(branch_id=my_branch)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )

    # Omitting branch_id → forced to own.
    r1 = await api_client.post(
        "/org/api/teams", json={"team_name": "Auto Branch"}
    )
    assert r1.status_code == 201
    assert r1.json()["branch_id"] == my_branch

    # Other branch_id → 404.
    r2 = await api_client.post(
        "/org/api/teams",
        json={"team_name": "Cross Branch", "branch_id": other_branch},
    )
    assert r2.status_code == 404


async def test_region_manager_sees_only_region_teams(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="rm-t@x.test", role="region_manager")
    org_id = creds["organization_id"]
    region_a = await _seed_region(api_session_factory, org_id, name="Region A")
    region_b = await _seed_region(api_session_factory, org_id, name="Region B")
    branch_a = await _seed_branch_in_region(api_session_factory, org_id, region_a, "BA")
    branch_b = await _seed_branch_in_region(api_session_factory, org_id, region_b, "BB")
    await _seed_team(api_session_factory, org_id, branch_id=branch_a, name="Team A")
    await _seed_team(api_session_factory, org_id, branch_id=branch_b, name="Team B")
    async with api_session_factory() as s:
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "region_manager",
            )
            .values(region_id=region_a)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/teams")
    names = {t["team_name"] for t in r.json()["teams"]}
    assert "Team A" in names
    assert "Team B" not in names


# ---------------------------------------------------------------------------
# Coach App's /api/teams is unaffected (private-coach path)
# ---------------------------------------------------------------------------


async def test_coach_app_teams_endpoint_still_works(authed_client: AsyncClient):
    """The legacy /api/teams (Coach App, private coaches) must keep working
    after Phase 1.5. Uses the existing `authed_client` fixture which already
    registers a default test user."""
    # POST a private-coach team via the Coach App's existing endpoint.
    r2 = await authed_client.post(
        "/api/teams",
        json={"team_name": "My Private Team"},
    )
    assert r2.status_code == 201, r2.text
    body = r2.json()
    assert body["team_name"] == "My Private Team"

    # GET via the same endpoint returns it.
    r3 = await authed_client.get("/api/teams")
    assert r3.status_code == 200
    teams = r3.json()
    # Endpoint returns a bare list (not wrapped).
    if isinstance(teams, dict):
        teams = teams.get("teams", [])
    names = {t["team_name"] for t in teams}
    assert "My Private Team" in names
