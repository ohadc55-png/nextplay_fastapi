"""Tests for /org/api/programs (Phase 11 — programs rollup view)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import update

pytestmark = pytest.mark.asyncio


async def test_programs_list_lean_shape_for_org_admin(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.programs import Program

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        s.add_all([
            Program(organization_id=org_id, name="A"),
            Program(organization_id=org_id, name="B"),
        ])
        await s.commit()

    r = await org_admin_client.get("/org/api/programs")
    assert r.status_code == 200
    names = [p["name"] for p in r.json()["programs"]]
    assert sorted(names) == ["A", "B"]
    # Lean response — no count fields.
    assert "region_count" not in r.json()["programs"][0]


async def test_programs_with_counts_rolls_up_regions_teams_players(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.players import Player
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        p_a = Program(organization_id=org_id, name="Program A")
        p_b = Program(organization_id=org_id, name="Program B")
        s.add_all([p_a, p_b])
        await s.flush()
        # Phase 12 — regions are shared geography (no program_id); program
        # is set directly on TeamProfile. We still use 3 regions and 3 teams
        # so the rollup math (regions in A = distinct regions with A's teams = 2)
        # matches the original intent.
        r_a1 = Region(organization_id=org_id, name="R-A1")
        r_a2 = Region(organization_id=org_id, name="R-A2")
        r_b1 = Region(organization_id=org_id, name="R-B1")
        s.add_all([r_a1, r_a2, r_b1])
        await s.flush()
        t_a1 = TeamProfile(
            organization_id=org_id, region_id=r_a1.id, program_id=p_a.id, team_name="T-A1",
        )
        t_a2 = TeamProfile(
            organization_id=org_id, region_id=r_a2.id, program_id=p_a.id, team_name="T-A2",
        )
        t_b1 = TeamProfile(
            organization_id=org_id, region_id=r_b1.id, program_id=p_b.id, team_name="T-B1",
        )
        s.add_all([t_a1, t_a2, t_b1])
        await s.flush()
        s.add_all([
            Player(organization_id=org_id, team_id=t_a1.id, name="PA1", active=True),
            Player(organization_id=org_id, team_id=t_a1.id, name="PA2", active=True),
            Player(organization_id=org_id, team_id=t_a2.id, name="PA3", active=True),
            Player(organization_id=org_id, team_id=t_b1.id, name="PB1", active=True),
            # Inactive — should not count
            Player(organization_id=org_id, team_id=t_a1.id, name="PA-out", active=False),
        ])
        await s.commit()

    r = await org_admin_client.get("/org/api/programs?with_counts=true")
    by_name = {p["name"]: p for p in r.json()["programs"]}

    assert by_name["Program A"]["region_count"] == 2
    assert by_name["Program A"]["team_count"] == 2
    assert by_name["Program A"]["player_count"] == 3   # active only
    assert by_name["Program B"]["region_count"] == 1
    assert by_name["Program B"]["team_count"] == 1
    assert by_name["Program B"]["player_count"] == 1


async def test_programs_pm_only_sees_their_program(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from src.models.programs import Program
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="pm-rollup@org.test", role="program_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        p_a = Program(organization_id=org_id, name="A")
        p_b = Program(organization_id=org_id, name="B")
        s.add_all([p_a, p_b])
        await s.flush()
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "program_manager",
            )
            .values(program_id=p_a.id)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/programs?with_counts=true")
    names = [p["name"] for p in r.json()["programs"]]
    assert names == ["A"]
