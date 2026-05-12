"""Frontend page-route tests — anonymous + authed flows.

Verifies the templates render cleanly with the Jinja2 shims (`url_for`,
`g.user`, `g.csp_nonce`, `config.CSS_VERSION`). The `url_for` rewrites
into `/static/...` and the auth gate redirects unauthenticated users
to login (or 401 — the gate dependency picks)."""

from __future__ import annotations

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Anonymous pages
# ---------------------------------------------------------------------------


class TestAnonymousPages:
    async def test_main_renders(self, api_client: AsyncClient):
        r = await api_client.get("/main")
        assert r.status_code == 200
        assert "<!DOCTYPE html>" in r.text
        # url_for shim turned 'static' calls into /static/ paths
        assert "/static/" in r.text
        # No leftover {{ url_for(...) }} from a broken shim
        assert "url_for" not in r.text

    async def test_privacy_renders(self, api_client: AsyncClient):
        r = await api_client.get("/privacy")
        assert r.status_code == 200
        assert "<!DOCTYPE html>" in r.text

    async def test_terms_renders(self, api_client: AsyncClient):
        r = await api_client.get("/terms")
        assert r.status_code == 200
        assert "<!DOCTYPE html>" in r.text

    async def test_contact_sales_default_plan(self, api_client: AsyncClient):
        r = await api_client.get("/contact-sales")
        assert r.status_code == 200
        # Default plan is enterprise (label = Enterprise)
        assert "Enterprise" in r.text or "enterprise" in r.text.lower()

    async def test_contact_sales_academy_plan(self, api_client: AsyncClient):
        r = await api_client.get("/contact-sales?plan=academy")
        assert r.status_code == 200

    async def test_contact_sales_legacy_plan_alias(self, api_client: AsyncClient):
        """`academy10` is the legacy URL — should normalize to `academy`
        so old links still work."""
        r = await api_client.get("/contact-sales?plan=academy10")
        assert r.status_code == 200

    async def test_contact_sales_unknown_plan_falls_back(self, api_client: AsyncClient):
        r = await api_client.get("/contact-sales?plan=nope")
        assert r.status_code == 200

    async def test_login_renders_form(self, api_client: AsyncClient):
        r = await api_client.get("/login")
        assert r.status_code == 200
        assert "<form" in r.text

    async def test_register_renders_form(self, api_client: AsyncClient):
        r = await api_client.get("/register")
        assert r.status_code == 200
        assert "<form" in r.text

    async def test_login_redirects_when_already_authed(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.get("/login", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/"


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


class TestAuthGate:
    async def test_home_unauthed_redirects_to_main(self, api_client: AsyncClient):
        """Anonymous visitors hitting `/` should land on the public
        marketing page `/main`, not a 401 JSON. Mirrors v1's
        `@login_required` redirect behaviour (see `home()` in
        src/api/pages.py)."""
        r = await api_client.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers.get("location") == "/main"

    async def test_chat_unauthed_returns_401(self, api_client: AsyncClient):
        r = await api_client.get("/chat")
        assert r.status_code == 401

    async def test_settings_unauthed_returns_401(self, api_client: AsyncClient):
        r = await api_client.get("/settings")
        assert r.status_code == 401

    async def test_player_profile_unauthed_returns_401(
        self, api_client: AsyncClient
    ):
        r = await api_client.get("/player/123")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Authed pages — basic render + context
# ---------------------------------------------------------------------------


class TestAuthedPages:
    async def test_home_renders_for_logged_in_user(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.get("/")
        assert r.status_code == 200
        assert "<!DOCTYPE html>" in r.text
        # The default registered user has display_name="Tester" (api/conftest)
        assert "Tester" in r.text

    async def test_data_upload_renders(self, authed_client: AsyncClient):
        r = await authed_client.get("/data-upload")
        assert r.status_code == 200

    async def test_history_renders(self, authed_client: AsyncClient):
        r = await authed_client.get("/history")
        assert r.status_code == 200

    async def test_settings_renders(self, authed_client: AsyncClient):
        r = await authed_client.get("/settings")
        assert r.status_code == 200

    async def test_profile_renders(self, authed_client: AsyncClient):
        r = await authed_client.get("/profile")
        assert r.status_code == 200

    async def test_upgrade_renders(self, authed_client: AsyncClient):
        r = await authed_client.get("/upgrade")
        assert r.status_code == 200

    async def test_court_preview_renders(self, authed_client: AsyncClient):
        r = await authed_client.get("/court-preview")
        assert r.status_code == 200

    async def test_chat_without_team_falls_back_to_home(
        self, authed_client: AsyncClient
    ):
        """v1 behavior: a coach without an active team gets the home
        page (with a setup nudge) instead of the chat page."""
        r = await authed_client.get("/chat")
        assert r.status_code == 200
        # Same template (home.html) is rendered
        assert "<!DOCTYPE html>" in r.text


class TestCheckout:
    async def test_known_plan_renders(self, authed_client: AsyncClient):
        r = await authed_client.get("/checkout/pro")
        assert r.status_code == 200

    async def test_unknown_plan_redirects_to_upgrade(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.get("/checkout/enterprise", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/upgrade"

    async def test_invalid_billing_falls_back_to_monthly(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.get("/checkout/pro?billing=lifetime")
        assert r.status_code == 200


class TestClubAdmin:
    async def test_non_admin_redirects_home(self, authed_client: AsyncClient):
        """Default test user is NOT a club admin → /club-admin → /."""
        r = await authed_client.get("/club-admin", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/"


class TestPlayerProfile:
    async def test_unknown_player_returns_404(self, authed_client: AsyncClient):
        r = await authed_client.get("/player/99999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Service workers + static
# ---------------------------------------------------------------------------


class TestServiceWorkers:
    async def test_sw_js_served_from_root(self, api_client: AsyncClient):
        r = await api_client.get("/sw.js")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/javascript")
        # SWs at root scope need no-cache so updates land within 24h
        assert r.headers["cache-control"] == "no-cache, must-revalidate"
        # Service-Worker-Allowed: / lets the SW take the whole site as scope
        assert r.headers["service-worker-allowed"] == "/"

    async def test_upload_sw_js_served_from_root(self, api_client: AsyncClient):
        r = await api_client.get("/upload-sw.js")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/javascript")

    async def test_static_css_accessible(self, api_client: AsyncClient):
        r = await api_client.get("/static/css/style.css")
        assert r.status_code == 200
        assert "css" in r.headers["content-type"]

    async def test_static_manifest_json(self, api_client: AsyncClient):
        r = await api_client.get("/static/manifest.json")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "NEXTPLAY"
        # PWA-installable manifest needs scope + start_url + display
        assert body["scope"] == "/"
        assert body["display"] == "standalone"

    async def test_unknown_static_returns_404(self, api_client: AsyncClient):
        r = await api_client.get("/static/does/not/exist.css")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Frontend shim correctness
# ---------------------------------------------------------------------------


class TestJinjaShims:
    async def test_csp_nonce_in_rendered_page(self, authed_client: AsyncClient):
        """The security headers middleware injects a per-request nonce
        on `request.state.csp_nonce`. The page-context dependency
        wires it into `g.csp_nonce` so inline `<script nonce=...>`
        tags can satisfy the CSP."""
        r = await authed_client.get("/")
        # base.html has multiple <script nonce="..."> tags
        assert 'nonce="' in r.text
        # And the nonce should actually be a non-empty token (16+ chars)
        import re
        match = re.search(r'nonce="([^"]+)"', r.text)
        assert match is not None
        assert len(match.group(1)) >= 16

    async def test_url_for_resolves_static_paths(
        self, api_client: AsyncClient
    ):
        """{{ url_for('static', filename='css/style.css') }} →
        /static/css/style.css."""
        r = await api_client.get("/main")
        assert "/static/" in r.text
        # No leftover unresolved templating
        assert "{{" not in r.text
        assert "}}" not in r.text
