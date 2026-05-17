"""Tests for /org/api/dashboard/role-stats + recent-activity (Phase 1.7)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import update

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
    # Phase 12 — "branches" tile retired, "programs" tile takes its slot.
    assert {"members", "regions", "programs", "teams", "players", "pending_invites"} <= tile_keys
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
    """Phase 12 — an RM is always tied to (region × program). RM scoped to
    region A × program P sees only A's teams in program P."""
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="rm@d.test", role="region_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        prog = Program(organization_id=org_id, name="P")
        s.add(prog)
        await s.flush()
        a = Region(organization_id=org_id, name="A")
        b = Region(organization_id=org_id, name="B")
        s.add_all([a, b])
        await s.flush()
        a_id, b_id, prog_id = a.id, b.id, prog.id
        # Two teams in A (both in our program), one in B.
        s.add(TeamProfile(
            organization_id=org_id, region_id=a_id, program_id=prog_id,
            team_name="T-A1",
        ))
        s.add(TeamProfile(
            organization_id=org_id, region_id=b_id, program_id=prog_id,
            team_name="T-B1",
        ))
        # RM is pinned to (region A × program P).
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "region_manager",
            )
            .values(region_id=a_id, program_id=prog_id)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/dashboard/role-stats")
    body = r.json()
    assert body["role"] == "region_manager"
    by_key = {t["key"]: t["value"] for t in body["tiles"]}
    assert by_key["teams"] == 1     # only T-A1 (region A × program P)


# ---------------------------------------------------------------------------
# region_manager — new region_id direct path on teams (Phase 5)
# ---------------------------------------------------------------------------


async def test_region_manager_counts_teams_via_region_id_direct(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """Phase 12 — RM stats use TeamProfile.region_id (+ program_id) only.
    Teams in the RM's (region × program) slice all count."""
    from src.models.players import Player
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="rm-direct@d.test", role="region_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        prog = Program(organization_id=org_id, name="P")
        s.add(prog)
        await s.flush()
        prog_id = prog.id
        region = Region(organization_id=org_id, name="R-Direct")
        s.add(region)
        await s.flush()
        region_id = region.id

        team_a = TeamProfile(
            organization_id=org_id, region_id=region_id, program_id=prog_id,
            team_name="Team A",
        )
        team_b = TeamProfile(
            organization_id=org_id, region_id=region_id, program_id=prog_id,
            team_name="Team B",
        )
        s.add_all([team_a, team_b])
        await s.flush()
        s.add(Player(
            organization_id=org_id, team_id=team_a.id,
            name="PA", active=True,
        ))
        s.add(Player(
            organization_id=org_id, team_id=team_b.id,
            name="PB", active=True,
        ))
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "region_manager",
            )
            .values(region_id=region_id, program_id=prog_id)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/dashboard/role-stats")
    body = r.json()
    by_key = {t["key"]: t["value"] for t in body["tiles"]}
    assert by_key["teams"] == 2, body
    assert by_key["players"] == 2


async def test_region_manager_response_carries_program_name(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """Phase 12 — an RM may be pinned to a single program via
    UserOrganization.program_id (regions are shared, so a region itself
    isn't program-scoped). The role-stats response carries program_id +
    program_name so the dashboard subtitle can read 'מנהל מחוז חיפה, בועטות'."""
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="rm-prog@d.test", role="region_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        prog = Program(organization_id=org_id, name="בועטות")
        s.add(prog)
        await s.flush()
        region = Region(organization_id=org_id, name="מרכז")
        s.add(region)
        await s.flush()
        # Pin BOTH region_id + program_id on the RM membership.
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "region_manager",
            )
            .values(region_id=region.id, program_id=prog.id)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/dashboard/role-stats")
    body = r.json()
    assert body["role"] == "region_manager"
    assert body.get("program_name") == "בועטות"
    assert body.get("program_id") is not None


# ---------------------------------------------------------------------------
# program_manager scoping (Phase 4)
# ---------------------------------------------------------------------------


async def test_program_manager_sees_program_subtree(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """A PM scoped to program A sees regions/teams/members/players in A only."""
    from src.models.players import Player
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="pm@d.test", role="program_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        prog_a = Program(organization_id=org_id, name="Program A")
        prog_b = Program(organization_id=org_id, name="Program B")
        s.add_all([prog_a, prog_b])
        await s.flush()
        prog_a_id, prog_b_id = prog_a.id, prog_b.id
        # Phase 12 — regions are shared geography (no program_id). A team's
        # program is set directly via TeamProfile.program_id.
        r_a1 = Region(organization_id=org_id, name="A1")
        r_a2 = Region(organization_id=org_id, name="A2")
        r_b1 = Region(organization_id=org_id, name="B1")
        s.add_all([r_a1, r_a2, r_b1])
        await s.flush()
        # 1 team per region in A (so the PM's "regions in program" count = 2),
        # 1 team in B1 (must not leak into the PM's view).
        t_a1 = TeamProfile(
            organization_id=org_id, region_id=r_a1.id,
            program_id=prog_a_id, team_name="T-A1",
        )
        t_a2 = TeamProfile(
            organization_id=org_id, region_id=r_a2.id,
            program_id=prog_a_id, team_name="T-A2",
        )
        t_b = TeamProfile(
            organization_id=org_id, region_id=r_b1.id,
            program_id=prog_b_id, team_name="T-B",
        )
        s.add_all([t_a1, t_a2, t_b])
        await s.flush()
        # 2 players in A's first team, 1 in B's — total players in A = 2.
        s.add(Player(organization_id=org_id, team_id=t_a1.id, name="PA1", active=True))
        s.add(Player(organization_id=org_id, team_id=t_a1.id, name="PA2", active=True))
        s.add(Player(organization_id=org_id, team_id=t_b.id, name="PB1", active=True))
        # Scope the PM to program A.
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "program_manager",
            )
            .values(program_id=prog_a_id)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/dashboard/role-stats")
    body = r.json()
    assert body["role"] == "program_manager"
    assert body["program_id"] == prog_a_id
    assert body["program_name"] == "Program A"
    by_key = {t["key"]: t["value"] for t in body["tiles"]}
    assert by_key["regions"] == 2   # A1 + A2 (distinct regions where A has teams)
    assert by_key["teams"] == 2     # T-A1 + T-A2
    assert by_key["players"] == 2   # PA1 + PA2


async def test_program_manager_without_program_returns_warning(
    api_client: AsyncClient, seed_org_admin,
):
    """A PM membership without a program_id (mis-seeded) returns the same
    empty-state envelope as RM without a region — no crash."""
    creds = await seed_org_admin(email="pm-no-prog@d.test", role="program_manager")
    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/dashboard/role-stats")
    body = r.json()
    assert body["role"] == "program_manager"
    assert body["tiles"] == []
    assert "warning" in body


async def test_dashboard_summary_includes_program_name_for_pm(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """The /summary endpoint surfaces program_name for the header banner."""
    from src.models.programs import Program
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="pm-hdr@d.test", role="program_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        p = Program(organization_id=org_id, name="בועטות")
        s.add(p)
        await s.flush()
        prog_id = p.id
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "program_manager",
            )
            .values(program_id=prog_id)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/dashboard/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["program_id"] == prog_id
    assert body["program_name"] == "בועטות"


# ---------------------------------------------------------------------------
# (Phase 12) branch_manager role retired — its dashboard tests were here.
# ---------------------------------------------------------------------------


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
