"""Phase 13 — verification suite for per-org slug URLs.

The tests adapt to the current `ORG_SLUG_URLS_ENABLED` setting. Some
assertions only make sense in one flag state and are skipped in the other.
Run the suite once with the flag OFF (legacy behavior preserved) and once
with the flag ON (slug routing active) to cover both deployment modes.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from src.core.config import settings

SLUG_FLAG_ON = settings.ORG_SLUG_URLS_ENABLED


# ---------------------------------------------------------------------------
# Reserved-slug validation (runs in BOTH flag states — A1 ships this)
# ---------------------------------------------------------------------------


def test_reserved_slug_rejected_by_validator():
    """A1 — slugs that would shadow top-level routes are rejected at create."""
    from src.core.exceptions import ValidationError
    from src.services.org_validators import validate_slug

    for reserved in ("admin", "api", "static", "org", "join", "login"):
        with pytest.raises(ValidationError) as exc:
            validate_slug(reserved)
        assert exc.value.code == "reserved_slug"


def test_valid_slug_passes():
    """Sha'ar Shivyon's slug should keep passing — make sure we didn't
    accidentally reserve a real-world name."""
    from src.services.org_validators import validate_slug
    assert validate_slug("shaar-shivyon") == "shaar-shivyon"
    assert validate_slug("hapoel-ta-youth") == "hapoel-ta-youth"


# ---------------------------------------------------------------------------
# Root-level aliases — `/join` and `/invite-accept` always work
# ---------------------------------------------------------------------------


async def test_root_join_alias_serves_page(api_client: AsyncClient):
    r = await api_client.get("/join?code=ABCD-1234")
    assert r.status_code == 200
    assert "code" in r.text.lower() or "קוד" in r.text


async def test_root_invite_accept_alias_serves_page(api_client: AsyncClient):
    r = await api_client.get("/invite-accept?token=fake")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Legacy /org/* paths (always — they're the back-compat layer)
# ---------------------------------------------------------------------------


async def test_legacy_org_login_still_works(api_client: AsyncClient):
    r = await api_client.get("/org/login")
    assert r.status_code == 200


async def test_legacy_org_join_serves_or_redirects(api_client: AsyncClient):
    """When flag OFF: /org/join serves the page directly.
    When flag ON: it ALSO serves directly (literal route wins over catch-all)."""
    r = await api_client.get("/org/join?code=ABCD-1234", follow_redirects=False)
    # Either 200 (literal route) or 301 (catch-all to /join) — both are valid.
    assert r.status_code in (200, 301)


# ---------------------------------------------------------------------------
# Flag-OFF behaviors (skipped when slug URLs are ON)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SLUG_FLAG_ON, reason="legacy /org/dashboard only when flag OFF")
async def test_flag_off_legacy_dashboard_serves_200(org_admin_client: AsyncClient):
    r = await org_admin_client.get("/org/dashboard")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Flag-ON behaviors (skipped when slug URLs are OFF)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SLUG_FLAG_ON, reason="slug routes only when flag ON")
async def test_flag_on_slug_url_serves_200(org_admin_client: AsyncClient):
    """`/<slug>/dashboard` is the canonical tenant URL when the flag is on."""
    url = org_admin_client.slug_url("/dashboard")
    assert url.startswith("/") and "/dashboard" in url
    assert url != "/org/dashboard"
    r = await org_admin_client.get(url)
    assert r.status_code == 200


@pytest.mark.skipif(not SLUG_FLAG_ON, reason="legacy catch-all only registered when flag ON")
async def test_flag_on_legacy_dashboard_301_to_slug(
    org_admin_client: AsyncClient,
):
    """`GET /org/dashboard` 301-redirects to `/<slug>/dashboard` so old
    bookmarks / shared links auto-converge on the canonical URL."""
    r = await org_admin_client.get("/org/dashboard", follow_redirects=False)
    assert r.status_code == 301
    target = r.headers["location"]
    assert target.endswith("/dashboard")
    assert "/org/dashboard" not in target


@pytest.mark.skipif(not SLUG_FLAG_ON, reason="404-cloak only meaningful when slug routes exist")
async def test_flag_on_wrong_slug_returns_404(org_admin_client: AsyncClient):
    """A user whose session is for org A typing `/<slug_b>/dashboard`
    sees 404 (cloak) — never confirms whether `<slug_b>` even exists."""
    r = await org_admin_client.get(
        "/some-other-org/dashboard", follow_redirects=False,
    )
    assert r.status_code == 404


@pytest.mark.skipif(not SLUG_FLAG_ON, reason="anon redirect target depends on slug routes")
async def test_flag_on_anon_visits_slug_url_redirects_to_login_with_next(
    api_client: AsyncClient,
):
    """Anonymous user typing `/<slug>/dashboard` → 302 `/org/login?next=...`
    so post-login they can land where they tried to go."""
    r = await api_client.get(
        "/shaar-shivyon/dashboard", follow_redirects=False,
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("/org/login")
    assert "next=" in loc


# ---------------------------------------------------------------------------
# Non-tenant top-level paths are NEVER swallowed by the slug router
# ---------------------------------------------------------------------------


async def test_static_path_not_treated_as_slug(api_client: AsyncClient):
    """`/static/*` keeps serving static files regardless of slug middleware."""
    r = await api_client.get("/static/css/org.css")
    assert r.status_code == 200
    assert "text/css" in r.headers.get("content-type", "")


async def test_admin_path_not_treated_as_slug(api_client: AsyncClient):
    """`/admin/*` is not org-scoped — the slug middleware ignores it."""
    # Without admin session this should be 401/302, NOT 404 from a "slug mismatch".
    r = await api_client.get("/admin/dashboard", follow_redirects=False)
    assert r.status_code in (200, 302, 401)
