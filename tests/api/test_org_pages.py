"""Tests for /org/* HTML page routes.

Verifies that each page redirects appropriately when the session is missing
or partial, and that fully-authenticated users see the dashboard HTML.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestOrgLoginPage:
    async def test_anon_sees_login_form(self, api_client: AsyncClient):
        r = await api_client.get("/org/login")
        assert r.status_code == 200
        assert 'action="/org/login"' in r.text
        assert "Enterprise" in r.text

    async def test_logged_in_user_redirects_to_dashboard(
        self, org_admin_client: AsyncClient
    ):
        r = await org_admin_client.get("/org/login", follow_redirects=False)
        assert r.status_code == 302
        # Phase 13 — dashboard URL is slug-aware (legacy or /<slug>).
        assert r.headers["location"] == org_admin_client.slug_url("/dashboard")


class TestOrgDashboardPage:
    async def test_anon_redirects_to_login(self, api_client: AsyncClient):
        r = await api_client.get("/org/dashboard", follow_redirects=False)
        assert r.status_code == 302
        # Phase 13 — login redirect preserves the original destination via
        # `?next=…`, so post-login we can land the user where they tried to go.
        assert r.headers["location"].startswith("/org/login")

    async def test_authed_user_sees_dashboard_html(
        self, org_admin_client: AsyncClient
    ):
        # Phase 13 — under flag ON, `/org/dashboard` 301-redirects to
        # `/<slug>/dashboard`, so we hit the slug-aware URL directly to
        # land on a 200 in both flag states.
        r = await org_admin_client.get(
            org_admin_client.slug_url("/dashboard"), follow_redirects=True,
        )
        assert r.status_code == 200
        # The org name appears in the navbar (Jinja escapes the apostrophe).
        assert "Sha&#39;ar Shivyon" in r.text
        # Role appears as the brand tag — the Hebrew label for org_admin.
        # Jinja escapes the literal `"` inside מנכ"ל to &#34;.
        assert "מנכ&#34;ל" in r.text
        # Phase 1.7 + Phase 12 refresh: tiles + charts + mini calendar are
        # hydrated client-side from JSON endpoints. Activity feed was retired
        # in Phase 12 (today's practices + charts replace it).
        assert "data-tiles" in r.text
        assert "data-practice-today" in r.text
        assert "data-mini-cal" in r.text


class TestOrgRoleSelectPage:
    async def test_anon_redirects_to_login(self, api_client: AsyncClient):
        r = await api_client.get("/org/role-select", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/org/login"

    async def test_single_role_user_redirects_to_dashboard(
        self, org_admin_client: AsyncClient
    ):
        r = await org_admin_client.get("/org/role-select", follow_redirects=False)
        assert r.status_code == 302
        # Phase 13 — dashboard URL is slug-aware.
        assert r.headers["location"] == org_admin_client.slug_url("/dashboard")


class TestOrgLogoutPage:
    async def test_get_logout_clears_and_redirects(
        self, org_admin_client: AsyncClient
    ):
        r = await org_admin_client.get("/org/logout", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/org/login"
        # Subsequent /org/api/me must now fail (401 — no session).
        r2 = await org_admin_client.get("/org/api/me")
        assert r2.status_code == 401
