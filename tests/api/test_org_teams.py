"""Tests for /org/api/teams/* (Phase 1.5).

Critical invariant: private-coach teams (organization_id IS NULL) NEVER
appear in org-context responses. The corresponding Coach App endpoint
(/api/teams) keeps working for them. Both invariants get coverage.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

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
    body = r.json()
    assert body["teams"] == []
    # Phase 7 — response also carries the echoed filter values.
    assert "filters" in body


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


# ---------------------------------------------------------------------------
# Phase 7 — filterable teams page
# ---------------------------------------------------------------------------


async def _seed_program_region_team(
    session_factory, org_id: int, *, program_name: str, region_name: str, team_name: str,
):
    """Phase 12 — regions are shared geography (no program_id), program is
    a direct attribute on TeamProfile."""
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    async with session_factory() as s:
        p = Program(organization_id=org_id, name=program_name)
        s.add(p)
        await s.flush()
        r = Region(organization_id=org_id, name=region_name)
        s.add(r)
        await s.flush()
        t = TeamProfile(
            organization_id=org_id, region_id=r.id, program_id=p.id,
            team_name=team_name,
        )
        s.add(t)
        await s.commit()
        return {"program_id": p.id, "region_id": r.id, "team_id": t.id}


async def test_teams_list_response_includes_program_and_region_enrichment(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    seeded = await _seed_program_region_team(
        api_session_factory, org_id,
        program_name="בועטות", region_name="מרכז", team_name="צוות א",
    )

    r = await org_admin_client.get("/org/api/teams")
    assert r.status_code == 200, r.text
    body = r.json()
    teams = body["teams"]
    target = next(t for t in teams if t["id"] == seeded["team_id"])
    assert target["program_id"] == seeded["program_id"]
    assert target["program_name"] == "בועטות"
    assert target["region_id"] == seeded["region_id"]
    assert target["region_name"] == "מרכז"


async def test_teams_filter_by_program_id(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    a = await _seed_program_region_team(
        api_session_factory, org_id,
        program_name="Program A", region_name="R-A", team_name="T-A",
    )
    b = await _seed_program_region_team(
        api_session_factory, org_id,
        program_name="Program B", region_name="R-B", team_name="T-B",
    )

    r = await org_admin_client.get(
        "/org/api/teams?program_id=" + str(a["program_id"]),
    )
    names = [t["team_name"] for t in r.json()["teams"]]
    assert "T-A" in names
    assert "T-B" not in names
    assert b  # silence unused-var lint


async def test_teams_filter_by_search_q(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    await _seed_program_region_team(
        api_session_factory, org_id,
        program_name="P1", region_name="R1", team_name="הפועל חיפה",
    )
    await _seed_program_region_team(
        api_session_factory, org_id,
        program_name="P2", region_name="R2", team_name="מכבי תל אביב",
    )

    r = await org_admin_client.get("/org/api/teams?q=" + "הפועל")
    names = [t["team_name"] for t in r.json()["teams"]]
    assert names == ["הפועל חיפה"]


async def test_teams_filter_by_coach_user_id(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.auth.password_service import hash_password
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.users import User

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        coach = User(
            email="coach-1@org.test",
            password_hash=hash_password("Sup3rSecure!"),
            display_name="מאמן א",
        )
        s.add(coach)
        await s.flush()
        prog = Program(organization_id=org_id, name="P")
        s.add(prog)
        await s.flush()
        region = Region(organization_id=org_id, name="R", program_id=prog.id)
        s.add(region)
        await s.flush()
        s.add(TeamProfile(
            organization_id=org_id, region_id=region.id,
            team_name="Mine", user_id=coach.id,
        ))
        s.add(TeamProfile(
            organization_id=org_id, region_id=region.id,
            team_name="Other", user_id=None,
        ))
        await s.commit()
        coach_id = coach.id

    r = await org_admin_client.get(
        "/org/api/teams?coach_user_id=" + str(coach_id),
    )
    names = [t["team_name"] for t in r.json()["teams"]]
    assert names == ["Mine"]


async def test_program_manager_teams_scoped_to_own_program(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from sqlalchemy import update

    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="pm-teams@org.test", role="program_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        prog_a = Program(organization_id=org_id, name="A")
        prog_b = Program(organization_id=org_id, name="B")
        s.add_all([prog_a, prog_b])
        await s.flush()
        r_a = Region(organization_id=org_id, name="R-A")
        r_b = Region(organization_id=org_id, name="R-B")
        s.add_all([r_a, r_b])
        await s.flush()
        s.add(TeamProfile(
            organization_id=org_id, region_id=r_a.id, program_id=prog_a.id, team_name="T-A",
        ))
        s.add(TeamProfile(
            organization_id=org_id, region_id=r_b.id, program_id=prog_b.id, team_name="T-B",
        ))
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "program_manager",
            )
            .values(program_id=prog_a.id)
        )
        await s.commit()
        prog_b_id = prog_b.id

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/teams")
    names = [t["team_name"] for t in r.json()["teams"]]
    assert names == ["T-A"]

    r2 = await api_client.get("/org/api/teams?program_id=" + str(prog_b_id))
    assert r2.json()["teams"] == []


# ---------------------------------------------------------------------------
# Phase 8 — team detail
# ---------------------------------------------------------------------------


async def test_team_detail_returns_roster_and_today_practice(
    org_admin_client: AsyncClient, api_session_factory,
):
    from datetime import date, datetime, timedelta

    from src.models.players import Player
    from src.models.practice_sessions import PracticeSession
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        prog = Program(organization_id=org_id, name="P")
        s.add(prog)
        await s.flush()
        region = Region(organization_id=org_id, name="R")
        s.add(region)
        await s.flush()
        team = TeamProfile(
            organization_id=org_id, region_id=region.id, program_id=prog.id,
            team_name="Falcons", league="Senior", division="A",
        )
        s.add(team)
        await s.flush()
        s.add(Player(
            organization_id=org_id, team_id=team.id, name="P-Active",
            active=True, number=7,
        ))
        s.add(Player(
            organization_id=org_id, team_id=team.id, name="P-Inactive",
            active=False, number=99,
        ))
        # Today's practice
        today_at_9 = datetime.combine(date.today(), datetime.min.time()).replace(hour=9)
        s.add(PracticeSession(
            team_id=team.id, organization_id=org_id, region_id=region.id,
            title="Morning practice", scheduled_at=today_at_9,
            duration_minutes=90, location="Court 1",
        ))
        # Yesterday's practice — should be excluded.
        s.add(PracticeSession(
            team_id=team.id, organization_id=org_id, region_id=region.id,
            title="Yesterday", scheduled_at=today_at_9 - timedelta(days=1),
        ))
        await s.commit()
        team_id = team.id

    r = await org_admin_client.get("/org/api/teams/" + str(team_id) + "/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["team"]["team_name"] == "Falcons"
    assert body["team"]["region_name"] == "R"
    assert body["team"]["program_name"] == "P"
    # Roster only includes active players.
    names = [p["name"] for p in body["roster"]]
    assert names == ["P-Active"]
    assert body["roster"][0]["number"] == 7
    # Today's practice present, yesterday excluded.
    practice_titles = [p["title"] for p in body["practices_today"]]
    assert practice_titles == ["Morning practice"]


async def test_team_detail_404_for_cross_org(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from src.models.teams import TeamProfile

    org_a = await seed_org_admin(email="a@x.test", org_slug="a-x", org_name="A")
    org_b = await seed_org_admin(email="b@x.test", org_slug="b-x", org_name="B")
    async with api_session_factory() as s:
        team_in_a = TeamProfile(
            organization_id=org_a["organization_id"], team_name="A-Team",
        )
        s.add(team_in_a)
        await s.commit()
        tid = team_in_a.id

    # Log in as B, try to view A's team.
    await api_client.post(
        "/org/login", json={"email": org_b["email"], "password": org_b["password"]}
    )
    r = await api_client.get("/org/api/teams/" + str(tid) + "/detail")
    assert r.status_code == 404


async def test_team_detail_404_for_pm_outside_program(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from sqlalchemy import update

    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="pm-team@org.test", role="program_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        prog_a = Program(organization_id=org_id, name="A")
        prog_b = Program(organization_id=org_id, name="B")
        s.add_all([prog_a, prog_b])
        await s.flush()
        r_b = Region(organization_id=org_id, name="R-B", program_id=prog_b.id)
        s.add(r_b)
        await s.flush()
        team_in_b = TeamProfile(
            organization_id=org_id, region_id=r_b.id, team_name="T-B",
        )
        s.add(team_in_b)
        await s.flush()
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "program_manager",
            )
            .values(program_id=prog_a.id)
        )
        await s.commit()
        tid = team_in_b.id

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/teams/" + str(tid) + "/detail")
    assert r.status_code == 404
