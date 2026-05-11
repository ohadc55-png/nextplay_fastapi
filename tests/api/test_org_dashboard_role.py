"""Tests for /org/api/dashboard/role-stats + recent-activity (Phase 1.7)."""

from __future__ import annotations

from sqlalchemy import update

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# org_admin tiles
# ---------------------------------------------------------------------------


async def test_org_admin_role_stats_shape(org_admin_client: AsyncClient):
    r = await org_admin_client.get("/org/api/dashboard/role-stats")
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "org_admin"
    tile_keys = {t["key"] for t in body["tiles"]}
    assert {"members", "regions", "branches", "teams", "players", "pending_invites"} <= tile_keys
    # Every tile has a label + value + href.
    for t in body["tiles"]:
        assert "label" in t and "value" in t and "href" in t


async def test_org_admin_counts_org_wide(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Adding rows to the actor's org bumps the counts."""
    from src.models.regions import Region
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        s.add(Region(organization_id=org_id, name="R1"))
        s.add(TeamProfile(organization_id=org_id, team_name="T1"))
        await s.commit()

    r = await org_admin_client.get("/org/api/dashboard/role-stats")
    by_key = {t["key"]: t["value"] for t in r.json()["tiles"]}
    assert by_key["regions"] == 1
    assert by_key["teams"] == 1


# ---------------------------------------------------------------------------
# region_manager scoping
# ---------------------------------------------------------------------------


async def test_region_manager_sees_only_region_counts(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """A region_manager scoped to region A sees only branches/teams in A."""
    from src.models.branches import Branch
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="rm@d.test", role="region_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        a = Region(organization_id=org_id, name="A")
        b = Region(organization_id=org_id, name="B")
        s.add_all([a, b])
        await s.flush()
        a_id, b_id = a.id, b.id
        # 2 branches in A, 1 in B
        ba1 = Branch(organization_id=org_id, region_id=a_id, name="BA1")
        ba2 = Branch(organization_id=org_id, region_id=a_id, name="BA2")
        bb1 = Branch(organization_id=org_id, region_id=b_id, name="BB1")
        s.add_all([ba1, ba2, bb1])
        await s.flush()
        # 1 team in A's first branch, 0 in B
        s.add(TeamProfile(organization_id=org_id, branch_id=ba1.id, team_name="T-A"))
        s.add(TeamProfile(organization_id=org_id, branch_id=bb1.id, team_name="T-B"))
        # Scope the region_manager to region A.
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "region_manager",
            )
            .values(region_id=a_id)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/dashboard/role-stats")
    body = r.json()
    assert body["role"] == "region_manager"
    by_key = {t["key"]: t["value"] for t in body["tiles"]}
    assert by_key["branches"] == 2  # only A's branches
    assert by_key["teams"] == 1     # only T-A


# ---------------------------------------------------------------------------
# branch_manager scoping
# ---------------------------------------------------------------------------


async def test_branch_manager_sees_only_branch_counts(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from src.models.branches import Branch
    from src.models.players import Player
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="bm@d.test", role="branch_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        b1 = Branch(organization_id=org_id, name="B1")
        b2 = Branch(organization_id=org_id, name="B2")
        s.add_all([b1, b2])
        await s.flush()
        b1_id = b1.id
        t1 = TeamProfile(organization_id=org_id, branch_id=b1.id, team_name="T1")
        t2 = TeamProfile(organization_id=org_id, branch_id=b2.id, team_name="T2")
        s.add_all([t1, t2])
        await s.flush()
        s.add(Player(organization_id=org_id, team_id=t1.id, name="P1", active=True))
        s.add(Player(organization_id=org_id, team_id=t1.id, name="P2", active=True))
        s.add(Player(organization_id=org_id, team_id=t2.id, name="P3", active=True))
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "branch_manager",
            )
            .values(branch_id=b1_id)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/dashboard/role-stats")
    by_key = {t["key"]: t["value"] for t in r.json()["tiles"]}
    assert by_key["teams"] == 1     # only T1
    assert by_key["players"] == 2   # only P1+P2


# ---------------------------------------------------------------------------
# coach scoping
# ---------------------------------------------------------------------------


async def test_coach_sees_only_own_counts(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from src.models.players import Player
    from src.models.teams import TeamProfile

    creds = await seed_org_admin(email="cc@d.test", role="coach")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        my_team = TeamProfile(
            organization_id=org_id, team_name="Mine", user_id=creds["user_id"],
        )
        other_team = TeamProfile(
            organization_id=org_id, team_name="Other", user_id=None,
        )
        s.add_all([my_team, other_team])
        await s.flush()
        s.add(Player(organization_id=org_id, team_id=my_team.id, name="A", active=True))
        s.add(Player(organization_id=org_id, team_id=my_team.id, name="B", active=True))
        s.add(Player(organization_id=org_id, team_id=other_team.id, name="C", active=True))
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/dashboard/role-stats")
    body = r.json()
    assert body["role"] == "coach"
    by_key = {t["key"]: t["value"] for t in body["tiles"]}
    assert by_key["teams"] == 1
    assert by_key["players"] == 2
    assert by_key["coach_app"] == "→"  # link tile, not a number


# ---------------------------------------------------------------------------
# Recent activity
# ---------------------------------------------------------------------------


async def test_recent_activity_org_admin_sees_all(
    org_admin_client: AsyncClient, api_session_factory,
):
    """After org_admin does some state-changing ops, the audit feed has them."""
    # Triggers `region.create` audit row.
    await org_admin_client.post("/org/api/regions", json={"name": "Activity Region"})
    r = await org_admin_client.get("/org/api/dashboard/recent-activity")
    assert r.status_code == 200
    actions = [ev["action"] for ev in r.json()["events"]]
    assert "region.create" in actions


async def test_recent_activity_coach_sees_only_self(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """A coach sees their own audit rows; not those of the org_admin."""
    from src.auth.password_service import hash_password
    from src.models.org_audit import OrgAuditLog
    from src.models.user_organizations import UserOrganization
    from src.models.users import User

    # Org with one admin (factory creates the user + org + membership).
    admin = await seed_org_admin(email="da@d.test", org_slug="d-x", org_name="D")
    org_id = admin["organization_id"]

    # Add a coach user in the same org — bypassing seed_org_admin so we
    # don't collide on the unique slug.
    async with api_session_factory() as s:
        coach_user = User(
            email="dc@d.test", password_hash=hash_password("Sup3rSecure!"),
            display_name="dc", email_verified=True,
        )
        s.add(coach_user)
        await s.flush()
        s.add(UserOrganization(
            user_id=coach_user.id, organization_id=org_id,
            role="coach", status="active",
        ))
        # Two audit rows: admin's region.create, coach's player.create.
        s.add(OrgAuditLog(
            organization_id=org_id,
            actor_user_id=admin["user_id"], actor_email=admin["email"],
            action="region.create", target_type="region", target_id=1,
        ))
        s.add(OrgAuditLog(
            organization_id=org_id,
            actor_user_id=coach_user.id, actor_email=coach_user.email,
            action="player.create", target_type="player", target_id=2,
        ))
        await s.commit()
        coach_user_id = coach_user.id

    await api_client.post(
        "/org/login", json={"email": "dc@d.test", "password": "Sup3rSecure!"}
    )
    r = await api_client.get("/org/api/dashboard/recent-activity")
    actors = {ev["actor_user_id"] for ev in r.json()["events"]}
    assert actors == {coach_user_id}
