"""Tests for /org/api/branches/* (Phase 1.3)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _make_region(org_admin_client: AsyncClient, name: str) -> int:
    r = await org_admin_client.post("/org/api/regions", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_list_empty(org_admin_client: AsyncClient):
    r = await org_admin_client.get("/org/api/branches")
    assert r.status_code == 200
    assert r.json() == {"branches": []}


async def test_create_with_region(org_admin_client: AsyncClient):
    region_id = await _make_region(org_admin_client, "מחוז צפון")
    r = await org_admin_client.post(
        "/org/api/branches",
        json={"name": "סניף חיפה", "region_id": region_id},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "סניף חיפה"
    assert body["region_id"] == region_id


async def test_create_without_region(org_admin_client: AsyncClient):
    r = await org_admin_client.post(
        "/org/api/branches", json={"name": "סניף ראשי"}
    )
    assert r.status_code == 201
    assert r.json()["region_id"] is None


async def test_patch_rename_and_move_region(org_admin_client: AsyncClient):
    r1 = await _make_region(org_admin_client, "Region 1")
    r2 = await _make_region(org_admin_client, "Region 2")
    create = await org_admin_client.post(
        "/org/api/branches", json={"name": "Branch X", "region_id": r1}
    )
    bid = create.json()["id"]

    r = await org_admin_client.patch(
        f"/org/api/branches/{bid}",
        json={"name": "Branch X v2", "region_id": r2},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Branch X v2"
    assert body["region_id"] == r2


async def test_delete(org_admin_client: AsyncClient):
    r1 = await _make_region(org_admin_client, "ר")
    create = await org_admin_client.post(
        "/org/api/branches", json={"name": "ב", "region_id": r1}
    )
    bid = create.json()["id"]
    r = await org_admin_client.delete(f"/org/api/branches/{bid}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r2 = await org_admin_client.get(f"/org/api/branches/{bid}")
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Conflict / validation
# ---------------------------------------------------------------------------


async def test_duplicate_name_conflict(org_admin_client: AsyncClient):
    await org_admin_client.post("/org/api/branches", json={"name": "B1"})
    r = await org_admin_client.post("/org/api/branches", json={"name": "B1"})
    assert r.status_code == 409
    assert r.json().get("code") == "duplicate_name"


async def test_region_from_other_org_returns_404(
    org_admin_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """If body.region_id points at another org's region, create returns 404."""
    from src.models.regions import Region

    other = await seed_org_admin(email="other@org.test", org_slug="other-org", org_name="Other")
    async with api_session_factory() as s:
        foreign = Region(organization_id=other["organization_id"], name="Foreign")
        s.add(foreign)
        await s.commit()
        foreign_id = foreign.id

    r = await org_admin_client.post(
        "/org/api/branches",
        json={"name": "B", "region_id": foreign_id},
    )
    assert r.status_code == 404


async def test_delete_branch_with_teams_blocked(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Branch with attached team_profile rows → 409 branch_has_teams."""
    from src.models.teams import TeamProfile

    create = await org_admin_client.post("/org/api/branches", json={"name": "BT"})
    bid = create.json()["id"]
    org_id = org_admin_client.org_seed["organization_id"]

    async with api_session_factory() as s:
        s.add(TeamProfile(
            organization_id=org_id,
            branch_id=bid,
            team_name="Demo Team",
        ))
        await s.commit()

    r = await org_admin_client.delete(f"/org/api/branches/{bid}")
    assert r.status_code == 409
    assert r.json().get("code") == "branch_has_teams"


# ---------------------------------------------------------------------------
# Cross-tenant 404
# ---------------------------------------------------------------------------


async def test_get_branch_from_other_org_returns_404(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from src.models.branches import Branch

    a = await seed_org_admin(email="ax@org.test", org_slug="ax", org_name="AX")
    b = await seed_org_admin(email="bx@org.test", org_slug="bx", org_name="BX")

    async with api_session_factory() as s:
        branch_a = Branch(organization_id=a["organization_id"], name="A's branch")
        s.add(branch_a)
        await s.commit()
        branch_a_id = branch_a.id

    login = await api_client.post(
        "/org/login", json={"email": b["email"], "password": b["password"]}
    )
    assert login.status_code == 200

    r = await api_client.get(f"/org/api/branches/{branch_a_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Role gating — region_manager
# ---------------------------------------------------------------------------


async def _seed_region_manager(seed_org_admin, api_session_factory):
    """Helper: seeds an org with one region_manager scoped to a fresh region.
    Returns (creds, region_id)."""
    from sqlalchemy import update

    from src.models.regions import Region
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="rmbx@org.test", role="region_manager")
    async with api_session_factory() as s:
        region = Region(organization_id=creds["organization_id"], name="RM region")
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
        return creds, region.id


async def test_region_manager_create_forced_to_own_region(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """A region_manager POSTing a branch is forced into their own region_id
    (even if they don't pass it). They cannot create in a different region."""
    from src.models.regions import Region

    creds, my_region_id = await _seed_region_manager(seed_org_admin, api_session_factory)
    async with api_session_factory() as s:
        other = Region(organization_id=creds["organization_id"], name="Other")
        s.add(other)
        await s.commit()
        other_id = other.id

    login = await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    assert login.status_code == 200

    # No region_id → forced to own.
    r = await api_client.post(
        "/org/api/branches", json={"name": "AutoForced"}
    )
    assert r.status_code == 201
    assert r.json()["region_id"] == my_region_id

    # Different region_id → 404.
    r2 = await api_client.post(
        "/org/api/branches",
        json={"name": "ShouldFail", "region_id": other_id},
    )
    assert r2.status_code == 404


async def test_region_manager_cannot_delete(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """DELETE /org/api/branches/{id} is org_admin-only; region_manager → 404."""
    from src.models.branches import Branch

    creds, region_id = await _seed_region_manager(seed_org_admin, api_session_factory)
    async with api_session_factory() as s:
        b = Branch(
            organization_id=creds["organization_id"],
            region_id=region_id,
            name="Theirs",
        )
        s.add(b)
        await s.commit()
        bid = b.id

    login = await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    assert login.status_code == 200

    r = await api_client.delete(f"/org/api/branches/{bid}")
    assert r.status_code == 404
