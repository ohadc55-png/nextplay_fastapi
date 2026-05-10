"""Tests for the Phase 0 org repositories.

Verifies the load-bearing OrgScopedRepository contract:
- list_for_org(None) returns [] (not the entire table)
- get_for_org(id, other_org) returns None (404-not-403 — no cross-org leak)
- list_for_user(None) returns [] (UserOrganizations defensive)

Plus basic CRUD smoke for each new repository.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.branches import Branch
from src.models.organizations import Organization
from src.models.regions import Region
from src.models.user_organizations import UserOrganization
from src.models.users import User
from src.repositories.branches_repo import BranchesRepository
from src.repositories.organizations_repo import OrganizationsRepository
from src.repositories.regions_repo import RegionsRepository
from src.repositories.user_organizations_repo import UserOrganizationsRepository

pytestmark = pytest.mark.asyncio


async def _seed_user(session: AsyncSession, email: str) -> User:
    u = User(email=email, display_name=email.split("@")[0])
    session.add(u)
    await session.flush()
    return u


async def _seed_org(session: AsyncSession, *, slug: str, name: str) -> Organization:
    o = Organization(slug=slug, name=name)
    session.add(o)
    await session.flush()
    return o


# ---------------------------------------------------------------------------
# OrganizationsRepository
# ---------------------------------------------------------------------------


class TestOrganizationsRepository:
    async def test_create_get_by_slug(self, db_session: AsyncSession):
        repo = OrganizationsRepository(db_session)
        org = await _seed_org(db_session, slug="shaar-shivyon", name="Sha'ar Shivyon")
        fetched = await repo.get_by_slug("shaar-shivyon")
        assert fetched is not None
        assert fetched.id == org.id

    async def test_get_by_slug_skips_soft_deleted(self, db_session: AsyncSession):
        repo = OrganizationsRepository(db_session)
        await _seed_org(db_session, slug="archived-org", name="Archived")
        ok = await repo.soft_delete(
            (await repo.get_by_slug("archived-org")).id  # type: ignore[union-attr]
        )
        assert ok is True
        assert await repo.get_by_slug("archived-org") is None

    async def test_list_active_excludes_archived(self, db_session: AsyncSession):
        repo = OrganizationsRepository(db_session)
        a = await _seed_org(db_session, slug="alpha", name="Alpha")
        await _seed_org(db_session, slug="beta", name="Beta")
        await repo.soft_delete(a.id)
        names = {o.name for o in await repo.list_active()}
        assert names == {"Beta"}


# ---------------------------------------------------------------------------
# OrgScopedRepository contract — verified via RegionsRepository
# ---------------------------------------------------------------------------


class TestOrgScopedRepositoryContract:
    async def test_list_for_org_returns_empty_when_org_id_is_none(
        self, db_session: AsyncSession
    ):
        repo = RegionsRepository(db_session)
        # Even with rows in the DB, None org_id must return [] (defensive).
        org = await _seed_org(db_session, slug="x", name="X")
        db_session.add(Region(organization_id=org.id, name="North"))
        await db_session.flush()
        assert await repo.list_for_org(None) == []

    async def test_get_for_org_returns_none_on_org_mismatch(
        self, db_session: AsyncSession
    ):
        repo = RegionsRepository(db_session)
        org_a = await _seed_org(db_session, slug="org-a", name="A")
        org_b = await _seed_org(db_session, slug="org-b", name="B")
        region = Region(organization_id=org_a.id, name="North")
        db_session.add(region)
        await db_session.flush()
        # Same id, wrong org → None (the 404-not-403 source).
        assert await repo.get_for_org(region.id, organization_id=org_a.id) is not None
        assert await repo.get_for_org(region.id, organization_id=org_b.id) is None

    async def test_list_for_org_filters_by_org(self, db_session: AsyncSession):
        repo = BranchesRepository(db_session)
        org_a = await _seed_org(db_session, slug="org-a-2", name="A")
        org_b = await _seed_org(db_session, slug="org-b-2", name="B")
        db_session.add_all([
            Branch(organization_id=org_a.id, name="Tel Aviv"),
            Branch(organization_id=org_b.id, name="Haifa"),
        ])
        await db_session.flush()
        a_branches = {b.name for b in await repo.list_for_org(org_a.id)}
        b_branches = {b.name for b in await repo.list_for_org(org_b.id)}
        assert a_branches == {"Tel Aviv"}
        assert b_branches == {"Haifa"}


# ---------------------------------------------------------------------------
# UserOrganizationsRepository
# ---------------------------------------------------------------------------


class TestUserOrganizationsRepository:
    async def test_list_for_user_returns_empty_when_user_id_is_none(
        self, db_session: AsyncSession
    ):
        repo = UserOrganizationsRepository(db_session)
        assert await repo.list_for_user(None) == []

    async def test_list_for_user_filters_by_active_status(
        self, db_session: AsyncSession
    ):
        repo = UserOrganizationsRepository(db_session)
        user = await _seed_user(db_session, "ceo@org.test")
        org = await _seed_org(db_session, slug="active-org", name="Active")
        db_session.add_all([
            UserOrganization(
                user_id=user.id, organization_id=org.id, role="org_admin", status="active"
            ),
            UserOrganization(
                user_id=user.id, organization_id=org.id, role="coach", status="suspended"
            ),
        ])
        await db_session.flush()
        active_only = await repo.list_for_user(user.id, only_active=True)
        all_memberships = await repo.list_for_user(user.id, only_active=False)
        assert {m.role for m in active_only} == {"org_admin"}
        assert {m.role for m in all_memberships} == {"org_admin", "coach"}

    async def test_get_active_by_role_returns_none_on_mismatch(
        self, db_session: AsyncSession
    ):
        repo = UserOrganizationsRepository(db_session)
        user = await _seed_user(db_session, "manager@org.test")
        org = await _seed_org(db_session, slug="role-test-org", name="RoleTest")
        db_session.add(
            UserOrganization(
                user_id=user.id, organization_id=org.id, role="branch_manager",
            )
        )
        await db_session.flush()
        # Right user, right org, wrong role → None.
        assert await repo.get_active(
            user_id=user.id, organization_id=org.id, role="org_admin",
        ) is None
        assert await repo.get_active(
            user_id=user.id, organization_id=org.id, role="branch_manager",
        ) is not None
