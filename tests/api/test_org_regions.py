"""Tests for /org/api/regions/* (Phase 1.3)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# org_admin happy paths
# ---------------------------------------------------------------------------


async def test_list_empty(org_admin_client: AsyncClient):
    r = await org_admin_client.get("/org/api/regions")
    assert r.status_code == 200
    body = r.json()
    assert body["regions"] == []
    assert "program_id_filter" in body


async def test_create_and_list(org_admin_client: AsyncClient):
    r = await org_admin_client.post("/org/api/regions", json={"name": "מחוז צפון"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "מחוז צפון"
    assert isinstance(body["id"], int)
    assert body["organization_id"] == org_admin_client.org_seed["organization_id"]

    r2 = await org_admin_client.get("/org/api/regions")
    assert r2.status_code == 200
    assert len(r2.json()["regions"]) == 1


async def test_get_detail(org_admin_client: AsyncClient):
    create = await org_admin_client.post("/org/api/regions", json={"name": "מרכז"})
    rid = create.json()["id"]
    r = await org_admin_client.get(f"/org/api/regions/{rid}")
    assert r.status_code == 200
    assert r.json()["name"] == "מרכז"


async def test_patch_rename(org_admin_client: AsyncClient):
    create = await org_admin_client.post("/org/api/regions", json={"name": "צפון"})
    rid = create.json()["id"]
    r = await org_admin_client.patch(
        f"/org/api/regions/{rid}", json={"name": "צפון 2"}
    )
    assert r.status_code == 200
    assert r.json()["name"] == "צפון 2"


async def test_delete(org_admin_client: AsyncClient):
    create = await org_admin_client.post("/org/api/regions", json={"name": "דרום"})
    rid = create.json()["id"]
    r = await org_admin_client.delete(f"/org/api/regions/{rid}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r2 = await org_admin_client.get(f"/org/api/regions/{rid}")
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Conflict / validation
# ---------------------------------------------------------------------------


async def test_duplicate_name_conflict(org_admin_client: AsyncClient):
    await org_admin_client.post("/org/api/regions", json={"name": "אזור A"})
    r = await org_admin_client.post("/org/api/regions", json={"name": "אזור A"})
    assert r.status_code == 409
    body = r.json()
    assert body.get("code") == "duplicate_name"


async def test_delete_region_with_branches_blocked(
    org_admin_client: AsyncClient, api_session_factory,
):
    """A region that still has branches can't be deleted (409 region_has_branches)."""
    from src.models.branches import Branch

    create = await org_admin_client.post("/org/api/regions", json={"name": "עם סניף"})
    rid = create.json()["id"]
    org_id = org_admin_client.org_seed["organization_id"]

    async with api_session_factory() as s:
        s.add(Branch(organization_id=org_id, region_id=rid, name="סניף ילדים"))
        await s.commit()

    r = await org_admin_client.delete(f"/org/api/regions/{rid}")
    assert r.status_code == 409
    assert r.json().get("code") == "region_has_branches"


# ---------------------------------------------------------------------------
# Cross-tenant isolation (404, NOT 403)
# ---------------------------------------------------------------------------


async def test_get_region_from_other_org_returns_404(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """org_admin of org B cannot read org A's region."""
    from src.models.regions import Region

    # Seed two orgs each with an org_admin.
    a = await seed_org_admin(email="a@org.test", org_slug="org-a", org_name="Org A")
    b = await seed_org_admin(email="b@org.test", org_slug="org-b", org_name="Org B")

    async with api_session_factory() as s:
        region_a = Region(organization_id=a["organization_id"], name="Region in A")
        s.add(region_a)
        await s.commit()
        region_a_id = region_a.id

    # Log in as B; try to read A's region.
    login = await api_client.post(
        "/org/login", json={"email": b["email"], "password": b["password"]}
    )
    assert login.status_code == 200

    r = await api_client.get(f"/org/api/regions/{region_a_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Role gating
# ---------------------------------------------------------------------------


async def test_region_manager_cannot_create(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """Only org_admin can POST /org/api/regions. region_manager → 404."""
    from src.models.regions import Region

    creds = await seed_org_admin(email="rm@org.test", role="region_manager")
    async with api_session_factory() as s:
        # region_manager needs an active region scope to even log in cleanly;
        # assign one (cross-shape test — region need not be real).
        from sqlalchemy import update

        from src.models.user_organizations import UserOrganization

        region = Region(organization_id=creds["organization_id"], name="My Region")
        s.add(region)
        await s.flush()
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.organization_id == creds["organization_id"],
                UserOrganization.role == "region_manager",
            )
            .values(region_id=region.id)
        )
        await s.commit()

    login = await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    assert login.status_code == 200

    r = await api_client.post("/org/api/regions", json={"name": "Cannot Create"})
    # require_role(org_admin) → NotFoundError(404) for any other role.
    assert r.status_code == 404


async def test_region_manager_sees_only_own_region(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """A region_manager scoped to region A sees [A], not [A, B]."""
    from sqlalchemy import update

    from src.models.regions import Region
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="rm2@org.test", role="region_manager")

    async with api_session_factory() as s:
        a = Region(organization_id=creds["organization_id"], name="A")
        b = Region(organization_id=creds["organization_id"], name="B")
        s.add_all([a, b])
        await s.flush()
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.organization_id == creds["organization_id"],
                UserOrganization.role == "region_manager",
            )
            .values(region_id=a.id)
        )
        await s.commit()
        a_id = a.id

    login = await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    assert login.status_code == 200

    r = await api_client.get("/org/api/regions")
    assert r.status_code == 200
    rows = r.json()["regions"]
    assert len(rows) == 1
    assert rows[0]["id"] == a_id
    assert rows[0]["name"] == "A"


# ---------------------------------------------------------------------------
# Phase 6 — program filter + region detail
# ---------------------------------------------------------------------------


async def test_create_region_with_program_id(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.programs import Program

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        p = Program(organization_id=org_id, name="בועטות")
        s.add(p)
        await s.commit()
        program_id = p.id

    r = await org_admin_client.post(
        "/org/api/regions", json={"name": "מחוז חיפה", "program_id": program_id},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["program_id"] == program_id


async def test_list_regions_filters_by_program(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        prog_a = Program(organization_id=org_id, name="Program A")
        prog_b = Program(organization_id=org_id, name="Program B")
        s.add_all([prog_a, prog_b])
        await s.flush()
        # Phase 12 — regions are shared geography (no program_id). We
        # signal "region belongs to program A" by putting at least one
        # team of program A in it. R-None has no teams at all → never shows.
        r_a1 = Region(organization_id=org_id, name="R-A1")
        r_a2 = Region(organization_id=org_id, name="R-A2")
        r_b1 = Region(organization_id=org_id, name="R-B1")
        r_none = Region(organization_id=org_id, name="R-None")
        s.add_all([r_a1, r_a2, r_b1, r_none])
        await s.flush()
        s.add_all([
            TeamProfile(
                organization_id=org_id, region_id=r_a1.id,
                program_id=prog_a.id, team_name="T-A1",
            ),
            TeamProfile(
                organization_id=org_id, region_id=r_a2.id,
                program_id=prog_a.id, team_name="T-A2",
            ),
            TeamProfile(
                organization_id=org_id, region_id=r_b1.id,
                program_id=prog_b.id, team_name="T-B1",
            ),
        ])
        await s.commit()
        prog_a_id = prog_a.id

    r = await org_admin_client.get(
        "/org/api/regions?program_id=" + str(prog_a_id)
    )
    body = r.json()
    names = [row["name"] for row in body["regions"]]
    assert sorted(names) == ["R-A1", "R-A2"]
    assert body["program_id_filter"] == prog_a_id


async def test_region_detail_returns_teams_via_direct_region_id(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Teams that use the new `team.region_id` FK appear in the detail
    payload even without a branch row."""
    from src.models.players import Player
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        prog = Program(organization_id=org_id, name="בועטות")
        s.add(prog)
        await s.flush()
        region = Region(organization_id=org_id, name="מרכז")
        s.add(region)
        await s.flush()
        team = TeamProfile(
            organization_id=org_id, region_id=region.id,
            program_id=prog.id, team_name="טבעת",
        )
        s.add(team)
        await s.flush()
        s.add(Player(organization_id=org_id, team_id=team.id, name="P1", active=True))
        s.add(Player(organization_id=org_id, team_id=team.id, name="P2", active=True))
        await s.commit()
        region_id = region.id

    r = await org_admin_client.get("/org/api/regions/" + str(region_id) + "/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["region"]["name"] == "מרכז"
    assert body["program"]["name"] == "בועטות"
    assert [t["team_name"] for t in body["teams"]] == ["טבעת"]
    assert body["practices_today"] == []


async def test_region_detail_404_for_pm_cross_program(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """A program_manager of program A can NOT view region detail of a
    region living under program B — 404 (cloaked)."""
    from sqlalchemy import update

    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="pm-cross@org.test", role="program_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        prog_a = Program(organization_id=org_id, name="A")
        prog_b = Program(organization_id=org_id, name="B")
        s.add_all([prog_a, prog_b])
        await s.flush()
        region_in_b = Region(
            organization_id=org_id, name="R-B", program_id=prog_b.id,
        )
        s.add(region_in_b)
        await s.flush()
        # Scope the PM to program A.
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "program_manager",
            )
            .values(program_id=prog_a.id)
        )
        await s.commit()
        rid = region_in_b.id

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/regions/" + str(rid) + "/detail")
    assert r.status_code == 404


async def test_program_manager_list_regions_only_their_program(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from sqlalchemy import update

    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="pm-list@org.test", role="program_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        prog_a = Program(organization_id=org_id, name="A")
        prog_b = Program(organization_id=org_id, name="B")
        s.add_all([prog_a, prog_b])
        await s.flush()
        # Phase 12 — regions are shared, but PM sees only the regions where
        # their program has at least one team. Seeding a team per (program,
        # region) cell reproduces "R-A1 belongs to A, R-B1 belongs to B".
        r_a1 = Region(organization_id=org_id, name="R-A1")
        r_b1 = Region(organization_id=org_id, name="R-B1")
        s.add_all([r_a1, r_b1])
        await s.flush()
        s.add_all([
            TeamProfile(
                organization_id=org_id, region_id=r_a1.id,
                program_id=prog_a.id, team_name="t-A",
            ),
            TeamProfile(
                organization_id=org_id, region_id=r_b1.id,
                program_id=prog_b.id, team_name="t-B",
            ),
        ])
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "program_manager",
            )
            .values(program_id=prog_a.id)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/regions")
    names = [row["name"] for row in r.json()["regions"]]
    assert names == ["R-A1"]
