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
) -> dict[str, Any]:
    """Per-page Jinja context for /org/* templates. Mirrors the admin pattern."""
    return page_context(request, user=None, extra=extra or {})


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
    state = _org_session_state(request)
    user_id, org_id, role = state["user_id"], state["org_id"], state["role"]

    if not isinstance(user_id, int):
        return RedirectResponse(url="/org/login", status_code=302)
    if not isinstance(org_id, int) or not isinstance(role, str):
        return RedirectResponse(url="/org/role-select", status_code=302)

    # Re-validate the membership so a tampered cookie can't reach the dashboard.
    user = await UsersRepository(db).get_active(user_id)
    if not user:
        request.session.pop(ORG_SESSION_KEY, None)
        return RedirectResponse(url="/org/login", status_code=302)

    membership = await UserOrganizationsRepository(db).get_active(
        user_id=user_id, organization_id=org_id, role=role,
    )
    if not membership:
        # 404 == not 403; redirect to login keeps UX consistent.
        return RedirectResponse(url="/org/role-select", status_code=302)

    org = await OrganizationsRepository(db).get(org_id)
    if not org:
        return RedirectResponse(url="/org/login", status_code=302)

    return templates.TemplateResponse(
        "org/dashboard.html",
        _org_context(
            request,
            extra={
                "organization": {"id": org.id, "name": org.name, "slug": org.slug},
                "role": role,
                "user_email": user.email,
                "user_display_name": user.display_name,
            },
        ),
    )


__all__ = ["router"]
