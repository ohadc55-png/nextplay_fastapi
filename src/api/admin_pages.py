"""Admin panel HTML page routes — separate from the JSON API.

The JSON API lives in `src/api/admin.py` (and `admin_tasks.py` /
`admin_emails.py`). This module renders the Jinja2 admin templates
under `frontend/templates/admin/` — same set of templates v1 used.

Auth model: session-based via Starlette `SessionMiddleware`. The
`/admin/login` POST handler in `src/api/admin.py` writes the email to
`request.session["admin_email"]` after a bcrypt password check; this
module's pages read it back via the `_admin_session_email` helper. A
missing or non-allowlisted email redirects to /admin/login.

This is deliberately decoupled from the coach JWT/cookie auth — admins
are a tiny audience, browser-only, and benefit from being a separate
trust domain.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import ADMIN_SESSION_KEY
from src.auth.admin_auth import is_admin_email
from src.core.database import get_db
from src.frontend import page_context, templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-pages"])


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _admin_session_email(request: Request) -> str | None:
    """Read the admin email from the signed session cookie. Returns
    None when the session is empty or doesn't match the allowlist."""
    email = (request.session.get(ADMIN_SESSION_KEY) or "").strip().lower()
    if not email or not is_admin_email(email):
        return None
    return email


def _require_admin(request: Request) -> str | RedirectResponse:
    """Page-level guard. Returns the admin email when valid, or a
    302 RedirectResponse to /admin/login when not — handlers should
    `if isinstance(...): return ...` early."""
    email = _admin_session_email(request)
    if email is None:
        return RedirectResponse(url="/admin/login", status_code=302)
    return email


def _admin_context(
    request: Request,
    *,
    active_tab: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-page Jinja context for the admin templates. Mirrors v1's
    `active_tab` pattern + bundles the same globals every authed page
    gets (so url_for / g.csp_nonce / config keep working)."""
    ctx = page_context(request, user=None, extra={
        "active_tab": active_tab,
        "admin_email": _admin_session_email(request),
        **(extra or {}),
    })
    return ctx


# ---------------------------------------------------------------------------
# Login / logout / index
# ---------------------------------------------------------------------------


@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request) -> Any:
    """Render the admin login form. Already-logged-in admins jump
    straight to the dashboard."""
    if _admin_session_email(request) is not None:
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return templates.TemplateResponse(
        "admin/login.html",
        _admin_context(request, active_tab="login", extra={"error": None}),
    )


@router.get("/admin/logout")
async def admin_logout_get(request: Request) -> RedirectResponse:
    """v1 admin/logout was a GET — keeping the same shape so
    bookmarks work. Clears the session and redirects to login."""
    request.session.pop(ADMIN_SESSION_KEY, None)
    return RedirectResponse(url="/admin/login", status_code=302)


@router.get("/admin")
@router.get("/admin/")
async def admin_index() -> RedirectResponse:
    """Bare /admin lands on the dashboard (the page-level guard kicks
    in there if the session is empty)."""
    return RedirectResponse(url="/admin/dashboard", status_code=302)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


# Same enums v1 uses for the task-priority / status pickers.
_TASK_STATUSES = ("pending", "in_progress", "blocked", "done")
_TASK_PRIORITIES = ("critical", "high", "medium", "low")
_TASK_TYPES = ("feature", "bug", "refactor", "ops", "docs", "research", "other")
_TASK_TAGS = ("frontend", "backend", "ai", "infra", "growth", "billing")


@router.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.admin import AdminTask
    from src.models.users import User

    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    total_users = (await db.execute(
        select(func.count()).select_from(User)
        .where(User.deleted_at.is_(None))
    )).scalar_one()
    new_7d = (await db.execute(
        select(func.count()).select_from(User)
        .where(User.deleted_at.is_(None), User.created_at >= week_ago)
    )).scalar_one()
    new_30d = (await db.execute(
        select(func.count()).select_from(User)
        .where(User.deleted_at.is_(None), User.created_at >= month_ago)
    )).scalar_one()

    task_counts: dict[str, int] = {}
    for p in _TASK_PRIORITIES:
        task_counts[p] = (await db.execute(
            select(func.count()).select_from(AdminTask)
            .where(AdminTask.status != "done", AdminTask.priority == p)
        )).scalar_one()
    open_total = sum(task_counts.values())

    # Use ISO date string for cross-dialect compare; AdminTask.due_date
    # is TEXT in v1 schema.
    today_iso = now.date().isoformat()
    overdue = (await db.execute(
        select(func.count()).select_from(AdminTask)
        .where(
            AdminTask.status != "done",
            AdminTask.due_date.is_not(None),
            AdminTask.due_date < today_iso,
        )
    )).scalar_one()

    return templates.TemplateResponse(
        "admin/dashboard.html",
        _admin_context(request, active_tab="dashboard", extra={
            "total_users": total_users,
            "new_7d": new_7d,
            "new_30d": new_30d,
            "task_counts": task_counts,
            "open_total": open_total,
            "overdue": overdue,
            "TASK_STATUSES": _TASK_STATUSES,
            "TASK_PRIORITIES": _TASK_PRIORITIES,
            "TASK_TYPES": _TASK_TYPES,
            "TASK_TAGS": _TASK_TAGS,
        }),
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.conversations import Conversation
    from src.models.plays import Play
    from src.models.scouting import ScoutingVideo
    from src.models.teams import TeamProfile
    from src.models.users import User

    now = datetime.now(UTC)

    base = select(func.count()).select_from(User).where(User.deleted_at.is_(None))
    total = (await db.execute(base)).scalar_one()
    new_today = (await db.execute(
        base.where(User.created_at >= now - timedelta(days=1))
    )).scalar_one()
    new_week = (await db.execute(
        base.where(User.created_at >= now - timedelta(days=7))
    )).scalar_one()
    new_month = (await db.execute(
        base.where(User.created_at >= now - timedelta(days=30))
    )).scalar_one()
    active_7d = (await db.execute(
        base.where(User.last_login_at.is_not(None),
                   User.last_login_at >= now - timedelta(days=7))
    )).scalar_one()

    # Plan distribution
    plan_rows = (await db.execute(
        select(User.subscription_plan, func.count())
        .where(User.deleted_at.is_(None))
        .group_by(User.subscription_plan)
    )).all()
    plan_map = {(p or "none"): n for p, n in plan_rows}

    # User list with per-user counts (chat sessions, videos, plays, teams).
    # One subquery per metric — SQLite-friendly.
    sub_chats = (
        select(Conversation.user_id, func.count(func.distinct(Conversation.session_id)).label("c"))
        .group_by(Conversation.user_id)
        .subquery()
    )
    sub_videos = (
        select(ScoutingVideo.user_id, func.count().label("c"))
        .group_by(ScoutingVideo.user_id)
        .subquery()
    )
    sub_plays = (
        select(Play.user_id, func.count().label("c"))
        .group_by(Play.user_id)
        .subquery()
    )
    sub_teams = (
        select(TeamProfile.user_id, func.count().label("c"))
        .group_by(TeamProfile.user_id)
        .subquery()
    )

    rows = (await db.execute(
        select(
            User.id, User.email, User.display_name, User.subscription_plan,
            User.trial_ends_at, User.last_login_at, User.created_at,
            func.coalesce(sub_chats.c.c, 0).label("chat_count"),
            func.coalesce(sub_videos.c.c, 0).label("video_count"),
            func.coalesce(sub_plays.c.c, 0).label("play_count"),
            func.coalesce(sub_teams.c.c, 0).label("team_count"),
        )
        .select_from(User)
        .outerjoin(sub_chats, sub_chats.c.user_id == User.id)
        .outerjoin(sub_videos, sub_videos.c.user_id == User.id)
        .outerjoin(sub_plays, sub_plays.c.user_id == User.id)
        .outerjoin(sub_teams, sub_teams.c.user_id == User.id)
        .where(User.deleted_at.is_(None))
        .order_by(User.created_at.desc().nulls_last())
    )).mappings().all()

    user_list = [dict(r) for r in rows]

    return templates.TemplateResponse(
        "admin/users.html",
        _admin_context(request, active_tab="users", extra={
            "total": total,
            "new_today": new_today,
            "new_week": new_week,
            "new_month": new_month,
            "active_7d": active_7d,
            "plan_map": plan_map,
            "users": user_list,
        }),
    )


# ---------------------------------------------------------------------------
# Other admin pages — render the template shell with the data the
# template expects. Most of these were complex aggregations in v1; we
# pass minimal placeholders so the page renders, and the heavy data
# fetching can be filled in over time. The /admin/api/* JSON endpoints
# (already ported in Phase 4) deliver the same data for any page that
# wants to switch to async fetch.
# ---------------------------------------------------------------------------


def _empty_render(
    request: Request,
    *,
    template: str,
    active_tab: str,
    extra: dict[str, Any],
) -> Any:
    """Helper to render an admin template with auth guard."""
    return templates.TemplateResponse(
        template,
        _admin_context(request, active_tab=active_tab, extra=extra),
    )


@router.get("/admin/api-costs", response_class=HTMLResponse)
async def admin_api_costs_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.analytics import ApiUsageLog

    now = datetime.now(UTC)

    # Token sums + cost estimate. v1 stores cost_usd directly per row.
    base = select(
        func.coalesce(func.sum(ApiUsageLog.prompt_tokens), 0),
        func.coalesce(func.sum(ApiUsageLog.completion_tokens), 0),
        func.coalesce(func.sum(ApiUsageLog.cost_usd), 0.0),
        func.count(),
    ).select_from(ApiUsageLog)

    total_pt, total_ct, total_cost, total_calls = (
        await db.execute(base)
    ).one()

    last_30d = await db.execute(
        base.where(ApiUsageLog.created_at >= now - timedelta(days=30))
    )
    last_30d_pt, last_30d_ct, last_30d_cost, _ = last_30d.one()

    return _empty_render(
        request,
        template="admin/api_costs.html",
        active_tab="api-costs",
        extra={
            "total_calls": total_calls,
            "total_prompt_tokens": total_pt,
            "total_completion_tokens": total_ct,
            "total_cost": float(total_cost or 0),
            "last_30d_cost": float(last_30d_cost or 0),
        },
    )


@router.get("/admin/feature-usage", response_class=HTMLResponse)
async def admin_feature_usage_page(request: Request) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard
    return _empty_render(
        request,
        template="admin/feature_usage.html",
        active_tab="feature-usage",
        extra={"page_views_30d": 0, "page_views_7d": 0, "events": []},
    )


@router.get("/admin/feedback", response_class=HTMLResponse)
async def admin_feedback_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.coach import Feedback

    rows = list((await db.execute(
        select(Feedback)
        .order_by(Feedback.created_at.desc().nulls_last())
        .limit(200)
    )).scalars().all())

    pos = sum(1 for f in rows if f.rating == 1)
    neg = sum(1 for f in rows if f.rating == -1)

    return _empty_render(
        request,
        template="admin/feedback.html",
        active_tab="feedback",
        extra={
            "feedback": rows,
            "total": len(rows),
            "positive": pos,
            "negative": neg,
            "ratio": (pos / len(rows) * 100) if rows else 0,
            "since": "all-time",
        },
    )


@router.get("/admin/geography", response_class=HTMLResponse)
async def admin_geography_page(request: Request) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard
    return _empty_render(
        request,
        template="admin/geography.html",
        active_tab="geography",
        extra={
            "countries": [],
            "total_users": 0,
            "located": 0,
            "located_pct": 0,
            "top_country": "—",
            "country_count": 0,
        },
    )


@router.get("/admin/user-activity", response_class=HTMLResponse)
async def admin_user_activity_page(request: Request) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard
    return _empty_render(
        request,
        template="admin/user_activity.html",
        active_tab="user-activity",
        extra={
            "users": [],
            "active_today": 0,
            "active_7d": 0,
            "active_30d": 0,
            "inactive_30d": 0,
            "total": 0,
        },
    )


@router.get("/admin/sales-inquiries", response_class=HTMLResponse)
async def admin_sales_inquiries_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.inquiries import SalesInquiry

    rows = list((await db.execute(
        select(SalesInquiry).order_by(SalesInquiry.created_at.desc().nulls_last())
    )).scalars().all())

    new = sum(1 for r in rows if (r.status or "new") == "new")
    contacted = sum(1 for r in rows if (r.status or "") == "contacted")

    return _empty_render(
        request,
        template="admin/sales_inquiries.html",
        active_tab="sales-inquiries",
        extra={
            "inquiries": rows,
            "total": len(rows),
            "new": new,
            "contacted": contacted,
            "won": sum(1 for r in rows if (r.status or "") == "won"),
            "lost": sum(1 for r in rows if (r.status or "") == "lost"),
        },
    )


@router.get("/admin/research-sources", response_class=HTMLResponse)
async def admin_research_sources_page(request: Request) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard
    # research_url_log is empty until research_tool actually runs in
    # production. Render with empty list — table shows "no data yet".
    return _empty_render(
        request,
        template="admin/research_sources.html",
        active_tab="research-sources",
        extra={"sources": [], "total": 0},
    )


@router.get("/admin/email-log", response_class=HTMLResponse)
async def admin_email_log_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.email import EmailLog

    rows = list((await db.execute(
        select(EmailLog)
        .order_by(EmailLog.created_at.desc().nulls_last())
        .limit(200)
    )).scalars().all())

    return _empty_render(
        request,
        template="admin/email_log.html",
        active_tab="email-log",
        extra={"emails": rows, "total": len(rows)},
    )


@router.get("/admin/email-lists", response_class=HTMLResponse)
async def admin_email_lists_page(request: Request) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard
    # email_lists.html is JS-driven (5 fetch calls) — we just render
    # the shell and the page hits /admin/api/mailing-lists/* itself.
    return _empty_render(
        request,
        template="admin/email_lists.html",
        active_tab="email-lists",
        extra={},
    )


@router.get("/admin/email-compose", response_class=HTMLResponse)
async def admin_email_compose_page(request: Request) -> Any:
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard
    return _empty_render(
        request,
        template="admin/email_compose.html",
        active_tab="email-compose",
        extra={},
    )


# ---------------------------------------------------------------------------
# Phase 1.8 — Org Creation Wizard pages
# ---------------------------------------------------------------------------


@router.get("/admin/orgs", response_class=HTMLResponse)
async def admin_orgs_list_page(
    request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    """Table of all organizations. JSON data comes from /admin/api/orgs
    (Phase 0). This page just renders the shell + the JS that hydrates."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard
    return templates.TemplateResponse(
        "admin/orgs/list.html",
        _admin_context(request, active_tab="orgs"),
    )


@router.get("/admin/orgs/wizard", response_class=HTMLResponse)
async def admin_orgs_wizard_page(request: Request) -> Any:
    """4-step Org Creation Wizard. POSTs to /admin/api/orgs/wizard/commit."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard
    return templates.TemplateResponse(
        "admin/orgs/wizard.html",
        _admin_context(request, active_tab="orgs"),
    )


@router.get("/admin/orgs/{org_id}", response_class=HTMLResponse)
async def admin_orgs_detail_page(
    org_id: int, request: Request, db: AsyncSession = Depends(get_db),
) -> Any:
    """Single-org admin view. Driven by /admin/api/orgs/{id} (Phase 0)."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard
    return templates.TemplateResponse(
        "admin/orgs/detail.html",
        _admin_context(request, active_tab="orgs", extra={"org_id": org_id}),
    )


__all__ = ["router"]
