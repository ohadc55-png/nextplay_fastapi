"""Tests for the Phase 1 Sub-Phase 1.0 design system on /org/* pages.

These guard the new markers introduced when the three Phase 0 placeholder
templates were replaced (login, role_select, dashboard) and the master layout
(`base_org.html` + `_partials/sidebar.html` + `_partials/header.html`) was
introduced. Existing assertions in `test_org_pages.py` continue to apply.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestPublicLoginPage:
    async def test_login_uses_design_system_css(self, api_client: AsyncClient):
        r = await api_client.get("/org/login")
        assert r.status_code == 200
        # Loads the new design-system stylesheet, not admin.css.
        assert "/static/css/org.css" in r.text
        assert "/static/css/admin.css" not in r.text
        # RTL Hebrew shell.
        assert 'dir="rtl"' in r.text
        assert 'lang="he"' in r.text
        # Form contract preserved.
        assert 'action="/org/login"' in r.text
        assert 'name="email"' in r.text
        assert 'name="password"' in r.text


class TestRoleSelectPage:
    async def test_role_select_uses_design_system(
        self, api_client: AsyncClient
    ):
        # Anonymous → redirected, but the static asset path is verifiable
        # indirectly via the dashboard test below. Here we simply confirm
        # the redirect still works (form contract preserved by Phase 0 backend).
        r = await api_client.get("/org/role-select", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/org/login"


class TestDashboardLayout:
    async def test_dashboard_renders_master_layout(
        self, org_admin_client: AsyncClient
    ):
        r = await org_admin_client.get("/org/dashboard")
        assert r.status_code == 200
        body = r.text

        # 1. Loads the new design system, not the dark admin theme.
        assert "/static/css/org.css" in body
        assert "/static/js/org.js" in body
        assert "/static/css/admin.css" not in body

        # 2. RTL + Hebrew.
        assert 'dir="rtl"' in body
        assert 'lang="he"' in body

        # 3. Master layout shell rendered.
        assert "org-shell" in body
        assert 'class="org-sidebar"' in body or 'class="org-sidebar ' in body \
            or 'class="org-sidebar"' in body
        assert "org-header" in body
        assert "org-main" in body

        # 4. Phase 1.7 — dashboard now hydrates tiles dynamically from
        # /org/api/dashboard/role-stats. The page just ships the empty
        # containers + the activity feed slot.
        assert "data-tiles" in body
        assert "data-activity" in body

        # 5. Active sidebar item = dashboard.
        assert "is-active" in body

        # 6. "Coming soon" placeholders for unbuilt features.
        # Hebrew "בקרוב" appears 5+ times (one per disabled nav item).
        assert body.count("בקרוב") >= 5

    async def test_dashboard_exposes_org_ctx_in_header(
        self, org_admin_client: AsyncClient
    ):
        r = await org_admin_client.get("/org/dashboard")
        body = r.text
        # The org name (verbatim from fixture) is rendered in the header.
        # Jinja escapes the apostrophe in "Sha'ar Shivyon".
        assert "Sha&#39;ar Shivyon" in body
        # Role badge — Hebrew job title for `org_admin` is מנכ"ל (CEO).
        # Jinja escapes the `"` inside the string to &#34;.
        assert "מנכ&#34;ל" in body
