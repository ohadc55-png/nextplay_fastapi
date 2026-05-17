"""Org Admin (`/org/*`) HTML page routes — separate from the JSON API.

JSON API: `src/api/org.py` (login dual-shape + `/org/api/*`).
This module: GET-only renders for login form, role selector, dashboard.

Auth: session-based via Starlette `SessionMiddleware`. The `/org/login` POST
in `src/api/org.py` writes the org session keys; this module reads them via
the helpers below. Pages without a valid session redirect to /org/login.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import (
    ORG_ACTIVE_ORG_KEY,
    ORG_ACTIVE_ROLE_KEY,
    ORG_SESSION_KEY,
)
from src.core.config import settings
from src.core.database import get_db
from src.core.exceptions import NotFoundError
from src.frontend import page_context, templates
from src.repositories.organizations_repo import OrganizationsRepository
from src.repositories.user_organizations_repo import UserOrganizationsRepository
from src.repositories.users_repo import UsersRepository

logger = logging.getLogger(__name__)

# Phase 13 — when `ORG_SLUG_URLS_ENABLED` is True, every tenant route lives
# at `/{org_slug}/<page>` and the slug is checked against the session's
# active org (404-cloak on mismatch). When False, the legacy `/org/<page>`
# layout is registered instead — existing prod behavior, zero change.
# The flag is read once at module load. Restart required to flip.
_TENANT_PREFIX = "/{org_slug}" if settings.ORG_SLUG_URLS_ENABLED else "/org"

# Phase 13 — split into TWO routers so main.py can register them at
# different positions in the route table:
#   1. `public_router`: pre-session / cross-session pages (login, logout,
#      role-select, invite-accept, join) + the new root aliases (/join,
#      /invite-accept) + the legacy `/org/{path:path}` catch-all.
#      Registered EARLY in main.py so the catch-all beats the parameterized
#      tenant_router for `/org/<anything>` paths.
#   2. `tenant_router`: the 15 actual tenant pages. Registered LAST in
#      main.py so literal-prefix routers (`/api/*`, `/admin/*`, etc.) win
#      against `/{org_slug}/X` matching (otherwise `/api/teams` would be
#      caught by `/{org_slug}/teams` with `org_slug="api"`).
#
# `router` is kept as the umbrella export so old `app.include_router(
# org_pages_router.router)` callers don't break — but main.py now imports
# the two sub-routers directly.
public_router = APIRouter(tags=["org-pages"])
tenant_router = APIRouter(tags=["org-pages"])
router = APIRouter(tags=["org-pages"])


# Hebrew display labels for technical role keys. Mirrors `_VALID_ROLES` in
# src/api/org.py — Phase 12 hierarchy: org_admin → program_manager →
# region_manager → coach (+ viewer as a read-only auditor).
ROLE_LABELS_HE: dict[str, str] = {
    "org_admin":       "מנכ\"ל",
    "program_manager": "מנהל תוכנית",
    "region_manager":  "מנהל מחוז",
    "coach":           "מאמן",
    "viewer":          "צופה",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _org_session_state(request: Request) -> dict[str, Any]:
    """Read the three Org session keys + return their parsed values. Used by
    every page guard to decide whether to redirect to login or role-select."""
    return {
        "user_id": request.session.get(ORG_SESSION_KEY),
        "org_id": request.session.get(ORG_ACTIVE_ORG_KEY),
        "role": request.session.get(ORG_ACTIVE_ROLE_KEY),
    }


def _tenant_url(org_slug: str, page: str) -> str:
    """Build a tenant URL respecting the slug-URLs flag.

    Returns `/<slug>/<page>` when the flag is ON, `/org/<page>` when OFF.
    Used by handlers that hand-craft breadcrumb hrefs (e.g. detail pages)
    so the back-link points at the right place under either layout.
    """
    page = page.lstrip("/")
    if settings.ORG_SLUG_URLS_ENABLED:
        return f"/{org_slug}/{page}"
    return f"/org/{page}"


def _org_context(
    request: Request,
    *,
    extra: dict[str, Any] | None = None,
    org_ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-page Jinja context for /org/* templates. Mirrors the admin pattern.

    `org_ctx` (Phase 1.0) carries the active-org bundle that the master layout
    + sidebar partials read: organization_name, role, user_display_name, etc.
    Pages that don't have an active session (login, role-select) pass None.
    """
    return page_context(request, user=None, org_ctx=org_ctx, extra=extra or {})


async def _require_active_org(
    request: Request,
    db: AsyncSession,
    org_slug: str | None = None,
) -> RedirectResponse | tuple[Any, Any, Any, dict[str, Any]]:
    """Shared boilerplate for every authenticated /org/* HTML page.

    Returns either:
    - a RedirectResponse if the session is missing/partial/stale (the caller
      just returns it), OR
    - a (user, membership, organization, org_ctx) tuple ready for template
      rendering.

    Phase 13: callers pass `org_slug` from the URL path when running under
    the slug-URL flag. We then 404-cloak any mismatch between the URL slug
    and the session's active org — never confirming whether the typed slug
    exists. When `org_slug` is None (legacy `/org/<page>` routes, flag OFF),
    the slug check is skipped.

    Redirect targets for anon / partial-session users preserve the original
    URL via `?next=…` so post-login we can land them where they tried to go.
    """
    state = _org_session_state(request)
    user_id, org_id, role = state["user_id"], state["org_id"], state["role"]

    # Re-build the path the user was trying to reach, so login → next can
    # return them there. Includes query string.
    qs = ("?" + request.url.query) if request.url.query else ""
    next_param = quote(request.url.path + qs, safe="")

    if not isinstance(user_id, int):
        return RedirectResponse(url=f"/org/login?next={next_param}", status_code=302)
    if not isinstance(org_id, int) or not isinstance(role, str):
        return RedirectResponse(url="/org/role-select", status_code=302)

    user = await UsersRepository(db).get_active(user_id)
    if not user:
        request.session.pop(ORG_SESSION_KEY, None)
        return RedirectResponse(url=f"/org/login?next={next_param}", status_code=302)

    membership = await UserOrganizationsRepository(db).get_active(
        user_id=user_id, organization_id=org_id, role=role,
    )
    if not membership:
        return RedirectResponse(url="/org/role-select", status_code=302)

    # Phase 14.7 — coaches live in the Coach App, not the org-admin surface.
    # Every tenant page (`/<slug>/dashboard`, `/<slug>/teams`, …) bounces
    # them to "/" so they can't reach a half-rendered admin view (the JSON
    # APIs already reject them with 404). Org session stays intact — the
    # in-Coach-App sidebar still shows their org name + program. Managers
    # (admin/PM/RM/BM) continue to the requested page.
    if role == "coach":
        return RedirectResponse(url="/", status_code=302)

    org = await OrganizationsRepository(db).get(org_id)
    if not org:
        return RedirectResponse(url="/org/login", status_code=302)

    # 404-cloak: if the URL has a slug that doesn't match the session's
    # active org, refuse the request. NEVER 403, NEVER auto-switch
    # (that would confirm whether the URL slug exists at all).
    if org_slug is not None and org.slug != org_slug:
        raise NotFoundError("Organization not found")

    membership_count = len(
        await UserOrganizationsRepository(db).list_for_user(user_id)
    )

    # Phase 12 — resolve scope context (program + region) and build a rich
    # role label that includes both. Examples:
    #   org_admin       → "מנהל ארגון"
    #   program_manager → "מנהל תוכנית סל-טק"
    #   region_manager  → "מנהל מחוז חיפה"  (cross-program)
    #   region_manager  → "מנהל מחוז חיפה, סל-טק"  (pinned to one program)
    # The labels are what the user sees in the top-right of every /org/* page,
    # so the scope is visible at a glance — critical when one user holds
    # multiple memberships and switches between them.
    from sqlalchemy import select as _select

    from src.models.programs import Program

    program_id_for_header: int | None = None
    program_name_for_header: str | None = None
    region_name_for_header: str | None = None

    if role == "program_manager":
        program_id_for_header = getattr(membership, "program_id", None)
    elif role == "region_manager":
        # An RM may optionally be pinned to a single program (PM-style
        # narrowing). Resolve both region name + program name for the label.
        program_id_for_header = getattr(membership, "program_id", None)
        if membership.region_id is not None:
            from src.models.regions import Region
            region_name_for_header = (await db.execute(
                _select(Region.name).where(Region.id == membership.region_id)
            )).scalar_one_or_none()

    if program_id_for_header is not None:
        program_name_for_header = (await db.execute(
            _select(Program.name).where(Program.id == program_id_for_header)
        )).scalar_one_or_none()

    base_label = ROLE_LABELS_HE.get(role, role)
    if role == "program_manager" and program_name_for_header:
        role_label_he = f"{base_label} {program_name_for_header}"
    elif role == "region_manager" and region_name_for_header:
        # "מחוז" appears in both the role label ("מנהל מחוז") and the region
        # name ("מחוז חיפה") — strip the redundant prefix so the label reads
        # "מנהל מחוז חיפה" instead of "מנהל מחוז מחוז חיפה".
        region_label = region_name_for_header
        if region_label.startswith("מחוז "):
            region_label = region_label[5:]
        if program_name_for_header:
            role_label_he = f"{base_label} {region_label}, {program_name_for_header}"
        else:
            role_label_he = f"{base_label} {region_label}"
    else:
        role_label_he = base_label

    # Phase 13 — single source of truth for tenant URL construction in
    # templates + JS. Either "/org" (legacy) or "/<slug>" depending on the
    # slug-URL flag. Templates use `{{ org_ctx.url_prefix }}/dashboard`
    # instead of hard-coded `/org/dashboard`, so flipping the flag in prod
    # rewrites every link without touching markup.
    url_prefix = f"/{org.slug}" if settings.ORG_SLUG_URLS_ENABLED else "/org"

    org_ctx = {
        "organization_id": org.id,
        "organization_name": org.name,
        "organization_slug": org.slug,
        "url_prefix": url_prefix,
        "role": role,
        "role_label_he": role_label_he,
        "program_id": program_id_for_header,
        "program_name": program_name_for_header,
        "region_id": membership.region_id,
        "region_name": region_name_for_header,
        "branch_id": membership.branch_id,
        "user_id": user.id,
        "user_email": user.email,
        "user_display_name": user.display_name or user.email,
        "has_multiple_roles": membership_count > 1,
    }
    return user, membership, org, org_ctx


# ---------------------------------------------------------------------------
# GET /org/login — login form
# ---------------------------------------------------------------------------


@public_router.get("/org/login", response_class=HTMLResponse)
async def org_login_page(
    request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    """Render the login form. Already-logged-in users with a single active
    membership go to /{slug}/dashboard; multi-role users go to /org/role-select.

    Phase 14.7 — exception: coaches see the form (not a redirect). A coach
    bouncing here from `/org` typically wants to sign in as a different
    account (e.g. their PM/admin alter-ego); the redirect-to-dashboard
    would loop them back into the Coach App via the `_require_active_org`
    coach-guard. Showing the form is harmless for a plain coach (they have
    no second account) and unblocks the multi-role case.
    """
    state = _org_session_state(request)
    role = state.get("role")
    if (
        isinstance(state["user_id"], int)
        and isinstance(state["org_id"], int)
        and role != "coach"
    ):
        # Resolve the active org's slug so the redirect goes straight to
        # the slug-prefixed dashboard when the flag is ON.
        org = await OrganizationsRepository(db).get(state["org_id"])
        target = _tenant_url(org.slug, "dashboard") if org else "/org/dashboard"
        return RedirectResponse(url=target, status_code=302)
    if isinstance(state["user_id"], int) and role != "coach":
        return RedirectResponse(url="/org/role-select", status_code=302)
    return templates.TemplateResponse(
        "org/login.html",
        _org_context(request, extra={"error": None}),
    )


# ---------------------------------------------------------------------------
# GET /org/logout — clear ORG keys + redirect (matches admin pattern)
# ---------------------------------------------------------------------------


@public_router.get("/org/logout")
async def org_logout_get(request: Request) -> RedirectResponse:
    request.session.pop(ORG_SESSION_KEY, None)
    request.session.pop(ORG_ACTIVE_ORG_KEY, None)
    request.session.pop(ORG_ACTIVE_ROLE_KEY, None)
    return RedirectResponse(url="/org/login", status_code=302)


# ---------------------------------------------------------------------------
# GET /org/role-select — multi-role users pick the active membership
# ---------------------------------------------------------------------------


@public_router.get("/org/role-select", response_class=HTMLResponse)
async def org_role_select_page(
    request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    state = _org_session_state(request)
    user_id = state["user_id"]
    if not isinstance(user_id, int):
        return RedirectResponse(url="/org/login", status_code=302)

    user = await UsersRepository(db).get_active(user_id)
    if not user:
        request.session.pop(ORG_SESSION_KEY, None)
        return RedirectResponse(url="/org/login", status_code=302)

    memberships = await UserOrganizationsRepository(db).list_for_user(user_id)
    if not memberships:
        # Stale session.
        request.session.pop(ORG_SESSION_KEY, None)
        return RedirectResponse(url="/org/login", status_code=302)
    if len(memberships) == 1:
        # Single-membership user — fast-path to their dashboard. Resolve
        # the slug so we land on /{slug}/dashboard under the slug-URL flag.
        only = memberships[0]
        org = await OrganizationsRepository(db).get(only.organization_id)
        target = _tenant_url(org.slug, "dashboard") if org else "/org/dashboard"
        return RedirectResponse(url=target, status_code=302)

    # Hydrate org names so the template can show them.
    repo = OrganizationsRepository(db)
    rows = []
    for m in memberships:
        org = await repo.get(m.organization_id)
        if org is None:
            continue
        rows.append({
            "organization_id": org.id,
            "organization_name": org.name,
            "organization_slug": org.slug,
            "role": m.role,
            "region_id": m.region_id,
            "branch_id": m.branch_id,
        })

    return templates.TemplateResponse(
        "org/role_select.html",
        _org_context(request, extra={"roles": rows, "user_email": user.email}),
    )


# ---------------------------------------------------------------------------
# GET /org/dashboard — basic dashboard
# ---------------------------------------------------------------------------


@tenant_router.get(f"{_TENANT_PREFIX}/dashboard", response_class=HTMLResponse)
async def org_dashboard_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    user, _membership, org, org_ctx = result
    return templates.TemplateResponse(
        "org/dashboard.html",
        _org_context(
            request,
            org_ctx=org_ctx,
            extra={
                "organization": {"id": org.id, "name": org.name, "slug": org.slug},
                "role": org_ctx["role"],
                "user_email": user.email,
                "user_display_name": user.display_name,
            },
        ),
    )


# ---------------------------------------------------------------------------
# GET /org/regions — Phase 1.3 page (uses /org/api/regions/* JSON)
# ---------------------------------------------------------------------------


@tenant_router.get(f"{_TENANT_PREFIX}/regions", response_class=HTMLResponse)
async def org_regions_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/regions.html",
        _org_context(request, org_ctx=org_ctx),
    )


@tenant_router.get(f"{_TENANT_PREFIX}/calendar", response_class=HTMLResponse)
async def org_calendar_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    """Month-grid calendar for practice sessions (Phase 12).
    Read-only for coaches; write authority for org_admin / PM / RM."""
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/calendar.html",
        _org_context(request, org_ctx=org_ctx),
    )


@tenant_router.get(f"{_TENANT_PREFIX}/programs", response_class=HTMLResponse)
async def org_programs_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    """Per-program rollup view (Phase 11). org_admin sees each program with
    aggregate region/team/coach/player counts and can drill into the
    regions list filtered to that program."""
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/programs.html",
        _org_context(request, org_ctx=org_ctx),
    )


@tenant_router.get(f"{_TENANT_PREFIX}/regions/{{region_id}}", response_class=HTMLResponse)
async def org_region_detail_page(
    region_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    """Per-region drill-down. JS hydrates from /org/api/regions/{id}/detail."""
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/region_detail.html",
        _org_context(request, org_ctx=org_ctx, extra={
            "region_id": region_id,
            "breadcrumb_items": [
                {"label": "מחוזות", "href": _tenant_url(_org.slug, "regions")},
                {"label_id": "crumb-region-name", "label": "טוען…"},
            ],
        }),
    )


@tenant_router.get(f"{_TENANT_PREFIX}/teams/{{team_id}}", response_class=HTMLResponse)
async def org_team_detail_page(
    team_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    """Per-team drill-down (Phase 8). JS hydrates from
    /org/api/teams/{id}/detail and offers inline roster CRUD."""
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/team_detail.html",
        _org_context(request, org_ctx=org_ctx, extra={
            "team_id": team_id,
            "breadcrumb_items": [
                {"label": "קבוצות", "href": _tenant_url(_org.slug, "teams")},
                {"label_id": "crumb-team-name", "label": "טוען…"},
            ],
        }),
    )


# ---------------------------------------------------------------------------
# (Phase 12) /org/branches routes removed — branches retired from the hierarchy.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /org/users — Phase 1.4 page (uses /org/api/users/* JSON)
# ---------------------------------------------------------------------------


@tenant_router.get(f"{_TENANT_PREFIX}/users", response_class=HTMLResponse)
async def org_users_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/users.html",
        _org_context(request, org_ctx=org_ctx),
    )


# ---------------------------------------------------------------------------
# GET /org/teams — Phase 1.5 page (uses /org/api/teams/* JSON)
# ---------------------------------------------------------------------------


@tenant_router.get(f"{_TENANT_PREFIX}/teams", response_class=HTMLResponse)
async def org_teams_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/teams.html",
        _org_context(request, org_ctx=org_ctx),
    )


# ---------------------------------------------------------------------------
# GET /org/players + /org/players/{id} — Phase 1.6 pages
# ---------------------------------------------------------------------------


@tenant_router.get(f"{_TENANT_PREFIX}/players", response_class=HTMLResponse)
async def org_players_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/players.html",
        _org_context(request, org_ctx=org_ctx),
    )


@tenant_router.get(f"{_TENANT_PREFIX}/players/{{player_id}}", response_class=HTMLResponse)
async def org_player_detail_page(
    player_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/player_detail.html",
        _org_context(request, org_ctx=org_ctx, extra={
            "player_id": player_id,
            "breadcrumb_items": [
                {"label": "שחקנים", "href": _tenant_url(_org.slug, "players")},
                {"label_id": "crumb-player-name", "label": "טוען…"},
            ],
        }),
    )


# ---------------------------------------------------------------------------
# Phase 2.2 — Document Templates HTML pages
# ---------------------------------------------------------------------------


@tenant_router.get(f"{_TENANT_PREFIX}/document-templates", response_class=HTMLResponse)
async def org_document_templates_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    """List page for templates. Data is loaded client-side via
    /org/api/document-templates/*."""
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/document_templates/list.html",
        _org_context(request, org_ctx=org_ctx),
    )


@tenant_router.get(f"{_TENANT_PREFIX}/document-templates/{{template_id}}/edit", response_class=HTMLResponse)
async def org_document_template_editor_page(
    template_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    """Field-marking editor. The JS pulls /preview?page=N as the canvas
    background and PATCHes /fields on save."""
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/document_templates/edit.html",
        _org_context(request, org_ctx=org_ctx, extra={"template_id": template_id}),
    )


@tenant_router.get(f"{_TENANT_PREFIX}/document-templates/{{template_id}}/deliveries", response_class=HTMLResponse)
async def org_document_template_deliveries_page(
    template_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    """Phase 2.5 — visibility page: "who signed / who didn't" for one template."""
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/document_templates/deliveries.html",
        _org_context(request, org_ctx=org_ctx, extra={"template_id": template_id}),
    )


# ---------------------------------------------------------------------------
# Phase 2.5 — Messaging Module HTML page
# ---------------------------------------------------------------------------


@tenant_router.get(f"{_TENANT_PREFIX}/messages", response_class=HTMLResponse)
async def org_messages_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    """Messaging module. Data is loaded client-side via /org/api/messages/*."""
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/messages/list.html",
        _org_context(request, org_ctx=org_ctx),
    )


# ---------------------------------------------------------------------------
# Phase 2.6a — Analytics dashboard
# ---------------------------------------------------------------------------


@tenant_router.get(f"{_TENANT_PREFIX}/analytics", response_class=HTMLResponse)
async def org_analytics_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    org_slug: str = "",
) -> Any:
    """Analytics dashboard. Data is loaded client-side via /org/api/analytics/*."""
    result = await _require_active_org(request, db, org_slug=org_slug or None)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/analytics.html",
        _org_context(request, org_ctx=org_ctx),
    )


# ---------------------------------------------------------------------------
# GET /org/invite-accept — PUBLIC page (token in query string).
# Renders the "set password + accept invite" form. POSTs to the existing
# /org/api/invites/accept JSON endpoint (Phase 0).
# ---------------------------------------------------------------------------


@public_router.get("/org/invite-accept", response_class=HTMLResponse)
async def org_invite_accept_page(request: Request) -> Any:
    """No session required — the token in `?token=` IS the credential.
    The actual accept happens via POST /org/api/invites/accept (Phase 0
    JSON endpoint); this page is just the HTML form."""
    token = request.query_params.get("token", "")
    return templates.TemplateResponse(
        "org/invite_accept.html",
        _org_context(request, extra={"invite_token": token}),
    )


@public_router.get("/org/join", response_class=HTMLResponse)
async def org_join_page(request: Request) -> Any:
    """Public self-service join page. The invitee types their 8-char code
    plus email + full name + password; the page POSTs to
    /org/api/invites/redeem and on success auto-redirects to /{slug}/dashboard."""
    prefill_code = request.query_params.get("code", "")
    return templates.TemplateResponse(
        "org/join.html",
        _org_context(request, extra={"prefill_code": prefill_code}),
    )


# ---------------------------------------------------------------------------
# Phase 13 — root-level aliases for the public token-based pages. The user
# pasting `nextplay.com/join` or clicking a fresh invite link shouldn't need
# to know the legacy `/org/` prefix exists.
# ---------------------------------------------------------------------------


@public_router.get("/join", response_class=HTMLResponse)
async def root_join_page(request: Request) -> Any:
    """Alias for /org/join (Phase 13). Same template + behavior — the join
    flow is org-agnostic at this stage (the 8-char code resolves the org)."""
    prefill_code = request.query_params.get("code", "")
    return templates.TemplateResponse(
        "org/join.html",
        _org_context(request, extra={"prefill_code": prefill_code}),
    )


@public_router.get("/invite-accept", response_class=HTMLResponse)
async def root_invite_accept_page(request: Request) -> Any:
    """Alias for /org/invite-accept (Phase 13). Token-based, org-agnostic."""
    token = request.query_params.get("token", "")
    return templates.TemplateResponse(
        "org/invite_accept.html",
        _org_context(request, extra={"invite_token": token}),
    )


# ---------------------------------------------------------------------------
# Phase 13 — legacy `/org/<path>` 301 catch-all. Lives forever so old
# bookmarks + already-sent invite links keep working. Specific literal
# routes (`/org/login`, `/org/logout`, `/org/role-select`, `/org/invite-accept`,
# `/org/join`) win over this catch-all by Starlette's literal-first matching,
# so the public pages keep working without a redirect hop.
#
# When the slug-URL flag is OFF, this handler is NOT registered — the
# legacy `/org/<page>` routes ARE the canonical surface and serve content
# directly. When ON, this catches everything else under `/org/` and
# redirects to the slug-prefixed equivalent.
# ---------------------------------------------------------------------------


if settings.ORG_SLUG_URLS_ENABLED:
    @public_router.get("/org/{path:path}", include_in_schema=False)
    async def org_legacy_redirect(
        path: str,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        """301 to `/<active_slug>/<path>`. Preserves query string. If the
        user has no session, lands them on `/org/login?next=…` so they can
        sign in and continue to the originally-requested URL."""
        qs = ("?" + request.url.query) if request.url.query else ""
        state = _org_session_state(request)

        if not isinstance(state["user_id"], int):
            # Anonymous — send to login with the destination preserved.
            next_url = quote(f"/org/{path}{qs}", safe="")
            return RedirectResponse(
                url=f"/org/login?next={next_url}", status_code=302,
            )
        if not isinstance(state["org_id"], int):
            return RedirectResponse(url="/org/role-select", status_code=302)

        org = await OrganizationsRepository(db).get(state["org_id"])
        if not org:
            # Org was soft-deleted while session was open. Clear + login.
            request.session.pop(ORG_SESSION_KEY, None)
            request.session.pop(ORG_ACTIVE_ORG_KEY, None)
            request.session.pop(ORG_ACTIVE_ROLE_KEY, None)
            return RedirectResponse(url="/org/login", status_code=302)

        return RedirectResponse(
            url=f"/{org.slug}/{path}{qs}", status_code=301,
        )


# Umbrella `router` keeps back-compat for tests / older code that imports
# `org_pages_router.router`. Includes public + tenant in the order needed
# inside this router (public first so the `/org/<page>` catch-all gets to
# run before tenant routes are tried within the umbrella).
router.include_router(public_router)
router.include_router(tenant_router)


__all__ = ["public_router", "router", "tenant_router"]
