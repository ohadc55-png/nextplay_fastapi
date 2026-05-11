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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import (
    ORG_ACTIVE_ORG_KEY,
    ORG_ACTIVE_ROLE_KEY,
    ORG_SESSION_KEY,
)
from src.core.database import get_db
from src.frontend import page_context, templates
from src.repositories.organizations_repo import OrganizationsRepository
from src.repositories.user_organizations_repo import UserOrganizationsRepository
from src.repositories.users_repo import UsersRepository

logger = logging.getLogger(__name__)

router = APIRouter(tags=["org-pages"])


# Hebrew display labels for technical role keys. The 5 keys here mirror
# `_VALID_ROLES` in src/api/org.py (org_admin / region_manager / branch_manager /
# coach / viewer). Sub-Phase 1.4 + 1.8 may add more roles — extend this map then.
ROLE_LABELS_HE: dict[str, str] = {
    "org_admin":      "מנכ\"ל",
    "region_manager": "מנהל מחוז",
    "branch_manager": "מנהל סניף",
    "coach":          "מאמן",
    "viewer":         "צופה",
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
    request: Request, db: AsyncSession,
) -> RedirectResponse | tuple[Any, Any, Any, dict[str, Any]]:
    """Shared boilerplate for every authenticated /org/* HTML page.

    Returns either:
    - a RedirectResponse if the session is missing/partial/stale (the caller
      just returns it), OR
    - a (user, membership, organization, org_ctx) tuple ready for template
      rendering.

    Phase 1.3+ pages all reuse this so we don't repeat the session-validation
    dance per route.
    """
    state = _org_session_state(request)
    user_id, org_id, role = state["user_id"], state["org_id"], state["role"]

    if not isinstance(user_id, int):
        return RedirectResponse(url="/org/login", status_code=302)
    if not isinstance(org_id, int) or not isinstance(role, str):
        return RedirectResponse(url="/org/role-select", status_code=302)

    user = await UsersRepository(db).get_active(user_id)
    if not user:
        request.session.pop(ORG_SESSION_KEY, None)
        return RedirectResponse(url="/org/login", status_code=302)

    membership = await UserOrganizationsRepository(db).get_active(
        user_id=user_id, organization_id=org_id, role=role,
    )
    if not membership:
        return RedirectResponse(url="/org/role-select", status_code=302)

    org = await OrganizationsRepository(db).get(org_id)
    if not org:
        return RedirectResponse(url="/org/login", status_code=302)

    membership_count = len(
        await UserOrganizationsRepository(db).list_for_user(user_id)
    )
    org_ctx = {
        "organization_id": org.id,
        "organization_name": org.name,
        "organization_slug": org.slug,
        "role": role,
        "role_label_he": ROLE_LABELS_HE.get(role, role),
        "region_id": membership.region_id,
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


@router.get("/org/login", response_class=HTMLResponse)
async def org_login_page(request: Request) -> Any:
    """Render the login form. Already-logged-in users with a single active
    membership go to /org/dashboard; multi-role users go to /org/role-select."""
    state = _org_session_state(request)
    if isinstance(state["user_id"], int) and isinstance(state["org_id"], int):
        return RedirectResponse(url="/org/dashboard", status_code=302)
    if isinstance(state["user_id"], int):
        return RedirectResponse(url="/org/role-select", status_code=302)
    return templates.TemplateResponse(
        "org/login.html",
        _org_context(request, extra={"error": None}),
    )


# ---------------------------------------------------------------------------
# GET /org/logout — clear ORG keys + redirect (matches admin pattern)
# ---------------------------------------------------------------------------


@router.get("/org/logout")
async def org_logout_get(request: Request) -> RedirectResponse:
    request.session.pop(ORG_SESSION_KEY, None)
    request.session.pop(ORG_ACTIVE_ORG_KEY, None)
    request.session.pop(ORG_ACTIVE_ROLE_KEY, None)
    return RedirectResponse(url="/org/login", status_code=302)


# ---------------------------------------------------------------------------
# GET /org/role-select — multi-role users pick the active membership
# ---------------------------------------------------------------------------


@router.get("/org/role-select", response_class=HTMLResponse)
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
        return RedirectResponse(url="/org/dashboard", status_code=302)

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


@router.get("/org/dashboard", response_class=HTMLResponse)
async def org_dashboard_page(
    request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    result = await _require_active_org(request, db)
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


@router.get("/org/regions", response_class=HTMLResponse)
async def org_regions_page(
    request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    result = await _require_active_org(request, db)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/regions.html",
        _org_context(request, org_ctx=org_ctx),
    )


# ---------------------------------------------------------------------------
# GET /org/branches — Phase 1.3 page (uses /org/api/branches/* JSON)
# ---------------------------------------------------------------------------


@router.get("/org/branches", response_class=HTMLResponse)
async def org_branches_page(
    request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    result = await _require_active_org(request, db)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/branches.html",
        _org_context(request, org_ctx=org_ctx),
    )


@router.get("/org/branches/{branch_id}", response_class=HTMLResponse)
async def org_branch_detail_page(
    branch_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Single-branch detail — hydrated by /org/api/branches/{id} +
    /org/api/teams?branch_id={id}. The detail page lists the teams in the
    branch and (via the existing teams endpoint) each team's coach."""
    result = await _require_active_org(request, db)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/branch_detail.html",
        _org_context(request, org_ctx=org_ctx, extra={"branch_id": branch_id}),
    )


# ---------------------------------------------------------------------------
# GET /org/users — Phase 1.4 page (uses /org/api/users/* JSON)
# ---------------------------------------------------------------------------


@router.get("/org/users", response_class=HTMLResponse)
async def org_users_page(
    request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    result = await _require_active_org(request, db)
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


@router.get("/org/teams", response_class=HTMLResponse)
async def org_teams_page(
    request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    result = await _require_active_org(request, db)
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


@router.get("/org/players", response_class=HTMLResponse)
async def org_players_page(
    request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    result = await _require_active_org(request, db)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/players.html",
        _org_context(request, org_ctx=org_ctx),
    )


@router.get("/org/players/{player_id}", response_class=HTMLResponse)
async def org_player_detail_page(
    player_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    result = await _require_active_org(request, db)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/player_detail.html",
        _org_context(request, org_ctx=org_ctx, extra={"player_id": player_id}),
    )


# ---------------------------------------------------------------------------
# Phase 2.2 — Document Templates HTML pages
# ---------------------------------------------------------------------------


@router.get("/org/document-templates", response_class=HTMLResponse)
async def org_document_templates_page(
    request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    """List page for templates. Data is loaded client-side via
    /org/api/document-templates/*."""
    result = await _require_active_org(request, db)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/document_templates/list.html",
        _org_context(request, org_ctx=org_ctx),
    )


@router.get("/org/document-templates/{template_id}/edit", response_class=HTMLResponse)
async def org_document_template_editor_page(
    template_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Field-marking editor. The JS pulls /preview?page=N as the canvas
    background and PATCHes /fields on save."""
    result = await _require_active_org(request, db)
    if isinstance(result, RedirectResponse):
        return result
    _user, _membership, _org, org_ctx = result
    return templates.TemplateResponse(
        "org/document_templates/edit.html",
        _org_context(request, org_ctx=org_ctx, extra={"template_id": template_id}),
    )


# ---------------------------------------------------------------------------
# GET /org/invite-accept — PUBLIC page (token in query string).
# Renders the "set password + accept invite" form. POSTs to the existing
# /org/api/invites/accept JSON endpoint (Phase 0).
# ---------------------------------------------------------------------------


@router.get("/org/invite-accept", response_class=HTMLResponse)
async def org_invite_accept_page(request: Request) -> Any:
    """No session required — the token in `?token=` IS the credential.
    The actual accept happens via POST /org/api/invites/accept (Phase 0
    JSON endpoint); this page is just the HTML form."""
    token = request.query_params.get("token", "")
    return templates.TemplateResponse(
        "org/invite_accept.html",
        _org_context(request, extra={"invite_token": token}),
    )


__all__ = ["router"]
