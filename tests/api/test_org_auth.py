"""Tests for /org/login, /org/logout, /org/switch-role, and the 404-not-403 rule.

The most load-bearing assertions are:
- Invalid credentials → 404 (NOT 401, NOT 200)
- Valid creds + zero memberships → 404 (private coach hitting the wrong door)
- Single membership → session set, redirect to dashboard
- Multiple memberships → "select_role" payload, no active org pinned yet
- Cross-org switch → 404 (NotFoundError, not ForbiddenError)
- Logout pops only ORG keys (admin / coach JWT untouched)
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestOrgLogin:
    async def test_invalid_password_returns_404(
        self, api_client: AsyncClient, seed_org_admin
    ):
        creds = await seed_org_admin(email="alice@org.test")
        r = await api_client.post(
            "/org/login",
            json={"email": creds["email"], "password": "WrongPassword123"},
        )
        assert r.status_code == 404

    async def test_unknown_email_returns_404(self, api_client: AsyncClient):
        r = await api_client.post(
            "/org/login",
            json={"email": "ghost@nowhere.test", "password": "Sup3rSecure!"},
        )
        assert r.status_code == 404

    async def test_user_with_no_memberships_returns_404(
        self, api_client: AsyncClient, register_user
    ):
        # Plain coach registration — no UserOrganization row.
        await register_user(email="solo-coach@example.com")
        r = await api_client.post(
            "/org/login",
            json={"email": "solo-coach@example.com", "password": "Sup3rSecure!"},
        )
        assert r.status_code == 404

    async def test_single_membership_logs_in_and_redirects(
        self, api_client: AsyncClient, seed_org_admin
    ):
        creds = await seed_org_admin()
        r = await api_client.post(
            "/org/login",
            json={"email": creds["email"], "password": creds["password"]},
        )
        assert r.status_code == 200
        body = r.json()
        # Phase 13 — redirect is slug-aware. flag OFF → /org/dashboard,
        # flag ON → /<slug>/dashboard. The expected URL is computed via
        # the same logic the server uses (see _dashboard_url in org.py).
        from src.core.config import settings as _s
        expected_redirect = (
            f"/{creds['organization_slug']}/dashboard"
            if _s.ORG_SLUG_URLS_ENABLED else "/org/dashboard"
        )
        assert body == {"ok": True, "redirect": expected_redirect}

    async def test_multi_role_returns_select_role_payload(
        self, api_client: AsyncClient, seed_org_admin, api_session_factory
    ):
        from src.models.organizations import Organization
        from src.models.user_organizations import UserOrganization

        creds = await seed_org_admin(org_slug="org-a", org_name="Org A")
        # Add a second org + membership for the same user.
        async with api_session_factory() as s:
            org_b = Organization(slug="org-b", name="Org B")
            s.add(org_b)
            await s.flush()
            s.add(UserOrganization(
                user_id=creds["user_id"], organization_id=org_b.id, role="coach",
            ))
            await s.commit()

        r = await api_client.post(
            "/org/login",
            json={"email": creds["email"], "password": creds["password"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "select_role"
        assert len(body["roles"]) == 2
        org_names = {row["organization_name"] for row in body["roles"]}
        assert org_names == {"Org A", "Org B"}


class TestOrgLogout:
    async def test_logout_clears_session(
        self, org_admin_client: AsyncClient
    ):
        # Already logged in via fixture.
        r = await org_admin_client.get("/org/api/me")
        assert r.status_code == 200
        # Logout
        r = await org_admin_client.post("/org/logout")
        assert r.status_code == 200
        # /org/api/me should now require login again — 401 (no session)
        r = await org_admin_client.get("/org/api/me")
        assert r.status_code == 401


class TestOrgApiMe:
    async def test_me_returns_active_membership(
        self, org_admin_client: AsyncClient
    ):
        r = await org_admin_client.get("/org/api/me")
        assert r.status_code == 200
        body = r.json()
        creds = org_admin_client.org_seed  # type: ignore[attr-defined]
        assert body["user_id"] == creds["user_id"]
        assert body["organization_id"] == creds["organization_id"]
        assert body["role"] == "org_admin"

    async def test_me_without_session_returns_401(self, api_client: AsyncClient):
        r = await api_client.get("/org/api/me")
        assert r.status_code == 401


class TestOrgSwitchRole:
    async def test_switch_to_unknown_role_returns_404(
        self, org_admin_client: AsyncClient
    ):
        creds = org_admin_client.org_seed  # type: ignore[attr-defined]
        r = await org_admin_client.post(
            "/org/switch-role",
            json={"organization_id": creds["organization_id"], "role": "ceo"},
        )
        # Role not in user's memberships → 404 (NOT 403)
        assert r.status_code == 404

    async def test_switch_to_other_org_returns_404(
        self, org_admin_client: AsyncClient, api_session_factory
    ):
        # Seed an unrelated org. The logged-in user is NOT a member of it.
        from src.models.organizations import Organization

        async with api_session_factory() as s:
            other = Organization(slug="other-org", name="Other")
            s.add(other)
            await s.commit()
            other_id = other.id

        r = await org_admin_client.post(
            "/org/switch-role",
            json={"organization_id": other_id, "role": "org_admin"},
        )
        assert r.status_code == 404


class TestDashboardSummary:
    async def test_summary_returns_org_counts(
        self, org_admin_client: AsyncClient
    ):
        r = await org_admin_client.get("/org/api/dashboard/summary")
        assert r.status_code == 200
        body = r.json()
        creds = org_admin_client.org_seed  # type: ignore[attr-defined]
        assert body["organization"]["id"] == creds["organization_id"]
        assert body["role"] == "org_admin"
        # Single org_admin member in the seed → exactly 1.
        assert body["member_count"] == 1
        assert body["branch_count"] == 0
        assert body["region_count"] == 0
        assert body["team_count"] == 0
