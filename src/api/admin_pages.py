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

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import ADMIN_SESSION_KEY
from src.auth.admin_auth import is_admin_email
from src.core.config import settings
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
    # User.created_at is stored as TEXT (legacy v1 schema). Comparing it
    # against a Python datetime works in SQLite (loose typing) but blows
    # up in Postgres with `operator does not exist: text >= timestamptz`.
    # Convert the cutoffs to ISO strings (space separator matches what
    # `func.now()` writes to the TEXT column) so both sides are TEXT.
    week_ago_iso = (now - timedelta(days=7)).isoformat(sep=" ")
    month_ago_iso = (now - timedelta(days=30)).isoformat(sep=" ")

    total_users = (await db.execute(
        select(func.count()).select_from(User)
        .where(User.deleted_at.is_(None))
    )).scalar_one()
    new_7d = (await db.execute(
        select(func.count()).select_from(User)
        .where(User.deleted_at.is_(None), User.created_at >= week_ago_iso)
    )).scalar_one()
    new_30d = (await db.execute(
        select(func.count()).select_from(User)
        .where(User.deleted_at.is_(None), User.created_at >= month_ago_iso)
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
    # User.created_at / last_login_at are TEXT in both SQLite (legacy) and
    # Postgres (matches v1's schema baseline). Comparing TEXT >= datetime
    # crashes Postgres with `operator does not exist: text >= timestamptz`,
    # so we serialise to the same ISO format that `func.now()::text` emits
    # ("YYYY-MM-DD HH:MM:SS"). Lex-compares correctly because the format
    # is fixed-width left-padded.
    day_ago_iso = (now - timedelta(days=1)).isoformat(sep=" ")
    week_ago_iso = (now - timedelta(days=7)).isoformat(sep=" ")
    month_ago_iso = (now - timedelta(days=30)).isoformat(sep=" ")

    base = select(func.count()).select_from(User).where(User.deleted_at.is_(None))
    total = (await db.execute(base)).scalar_one()
    new_today = (await db.execute(
        base.where(User.created_at >= day_ago_iso)
    )).scalar_one()
    new_week = (await db.execute(
        base.where(User.created_at >= week_ago_iso)
    )).scalar_one()
    new_month = (await db.execute(
        base.where(User.created_at >= month_ago_iso)
    )).scalar_one()
    active_7d = (await db.execute(
        base.where(User.last_login_at.is_not(None),
                   User.last_login_at >= week_ago_iso)
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
    """OpenAI spend dashboard: today/week/month rollups, daily trend,
    breakdown by model/agent/user, token split, monthly projection.
    Ports v1's `backend/admin/routes.py:api_costs`."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.crew.llm import MODEL_PRICING, _resolve_pricing
    from src.models.analytics import ApiUsageLog
    from src.models.conversations import Conversation
    from src.models.users import User

    now = datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # ── Window rollups: cost (today/week/month) + tokens (today) ──
    cost_today = float((await db.execute(
        select(func.coalesce(func.sum(ApiUsageLog.cost_usd), 0.0))
        .where(ApiUsageLog.created_at >= day_ago)
    )).scalar_one() or 0)
    cost_week = float((await db.execute(
        select(func.coalesce(func.sum(ApiUsageLog.cost_usd), 0.0))
        .where(ApiUsageLog.created_at >= week_ago)
    )).scalar_one() or 0)
    cost_month = float((await db.execute(
        select(func.coalesce(func.sum(ApiUsageLog.cost_usd), 0.0))
        .where(ApiUsageLog.created_at >= month_ago)
    )).scalar_one() or 0)
    tokens_today = int((await db.execute(
        select(func.coalesce(func.sum(ApiUsageLog.total_tokens), 0))
        .where(ApiUsageLog.created_at >= day_ago)
    )).scalar_one() or 0)

    # ── Daily trend (last 30d) ──
    daily_costs = [
        {"day": str(d), "cost": float(c or 0)}
        for d, c in (await db.execute(
            select(
                func.date(ApiUsageLog.created_at).label("day"),
                func.sum(ApiUsageLog.cost_usd).label("cost"),
            )
            .where(ApiUsageLog.created_at >= month_ago)
            .group_by(func.date(ApiUsageLog.created_at))
            .order_by(func.date(ApiUsageLog.created_at))
        )).all()
    ]

    # ── By model ──
    by_model = [
        {"model": m, "cost": float(c or 0), "tokens": int(t or 0)}
        for m, c, t in (await db.execute(
            select(
                ApiUsageLog.model,
                func.sum(ApiUsageLog.cost_usd).label("cost"),
                func.sum(ApiUsageLog.total_tokens).label("tokens"),
            )
            .group_by(ApiUsageLog.model)
            .order_by(func.sum(ApiUsageLog.cost_usd).desc())
        )).all()
    ]

    # ── By agent ──
    by_agent = [
        {"agent_key": ak, "cost": float(c or 0), "calls": int(n or 0)}
        for ak, c, n in (await db.execute(
            select(
                ApiUsageLog.agent_key,
                func.sum(ApiUsageLog.cost_usd).label("cost"),
                func.count().label("calls"),
            )
            .where(ApiUsageLog.agent_key.is_not(None))
            .group_by(ApiUsageLog.agent_key)
            .order_by(func.sum(ApiUsageLog.cost_usd).desc())
        )).all()
    ]

    # ── Top users by spend ──
    top_users = [
        {"email": e, "display_name": dn, "total_cost": float(c or 0), "total_tokens": int(t or 0)}
        for e, dn, c, t in (await db.execute(
            select(
                User.email,
                User.display_name,
                func.sum(ApiUsageLog.cost_usd).label("total_cost"),
                func.sum(ApiUsageLog.total_tokens).label("total_tokens"),
            )
            .join(User, ApiUsageLog.user_id == User.id)
            .group_by(ApiUsageLog.user_id, User.email, User.display_name)
            .order_by(func.sum(ApiUsageLog.cost_usd).desc())
            .limit(10)
        )).all()
    ]

    # ── Monthly burn projection (avg daily over 7d × 30) ──
    avg_daily_7d = float((await db.execute(
        select(func.coalesce(func.sum(ApiUsageLog.cost_usd), 0.0) / 7.0)
        .where(ApiUsageLog.created_at >= week_ago)
    )).scalar_one() or 0)
    monthly_projection = round(avg_daily_7d * 30, 2)

    # ── Input vs output token split (today + month) ──
    today_split = (await db.execute(
        select(
            func.coalesce(func.sum(ApiUsageLog.prompt_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(ApiUsageLog.completion_tokens), 0).label("output_tokens"),
        )
        .where(ApiUsageLog.created_at >= day_ago)
    )).one()
    tokens_split_today = {
        "input_tokens": int(today_split.input_tokens or 0),
        "output_tokens": int(today_split.output_tokens or 0),
    }
    month_split = (await db.execute(
        select(
            func.coalesce(func.sum(ApiUsageLog.prompt_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(ApiUsageLog.completion_tokens), 0).label("output_tokens"),
        )
        .where(ApiUsageLog.created_at >= month_ago)
    )).one()
    tokens_split_month = {
        "input_tokens": int(month_split.input_tokens or 0),
        "output_tokens": int(month_split.output_tokens or 0),
    }

    # ── Cost per chat session (last 30d) ──
    # Conversation.created_at is DateTime — compare directly.
    total_sessions_30d = (await db.execute(
        select(func.count(distinct(Conversation.session_id)))
        .where(Conversation.created_at >= month_ago)
    )).scalar_one() or 0
    cost_per_session = round(cost_month / total_sessions_30d, 4) if total_sessions_30d else 0

    # ── Per-agent detail with model + token split ──
    # Pre-resolve pricing per row so the template doesn't have to know
    # about dated OpenAI model variants. `price_input` / `price_output`
    # are per-1M-tokens — matches the columns the template renders.
    agent_detail = []
    for ak, m, pt, ct, c, n in (await db.execute(
        select(
            ApiUsageLog.agent_key,
            ApiUsageLog.model,
            func.sum(ApiUsageLog.prompt_tokens),
            func.sum(ApiUsageLog.completion_tokens),
            func.sum(ApiUsageLog.cost_usd),
            func.count(),
        )
        .where(ApiUsageLog.agent_key.is_not(None))
        .group_by(ApiUsageLog.agent_key, ApiUsageLog.model)
        .order_by(func.sum(ApiUsageLog.cost_usd).desc())
    )).all():
        pricing = _resolve_pricing(m)
        agent_detail.append({
            "agent_key": ak, "model": m,
            "price_input": pricing["input"],
            "price_output": pricing["output"],
            "input_tokens": int(pt or 0),
            "output_tokens": int(ct or 0),
            "cost": float(c or 0),
            "calls": int(n or 0),
        })

    return _empty_render(
        request,
        template="admin/api_costs.html",
        active_tab="api_costs",
        extra={
            "has_table": True,
            "model_pricing": MODEL_PRICING,
            "cost_today": cost_today,
            "cost_week": cost_week,
            "cost_month": cost_month,
            "tokens_today": tokens_today,
            "daily_costs": daily_costs,
            "by_model": by_model,
            "by_agent": by_agent,
            "top_users": top_users,
            "monthly_projection": monthly_projection,
            "tokens_split_today": tokens_split_today,
            "tokens_split_month": tokens_split_month,
            "cost_per_session": cost_per_session,
            "agent_detail": agent_detail,
        },
    )


@router.get("/admin/feature-usage", response_class=HTMLResponse)
async def admin_feature_usage_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Feature adoption dashboard: per-feature totals, agent usage,
    30-day daily message volume, adoption percentages per feature.
    Ports v1's `backend/admin/routes.py:feature_usage`."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.conversations import Conversation
    from src.models.plays import Play
    from src.models.scouting import ScoutingVideo
    from src.models.uploads import Upload
    from src.models.users import User

    # Per-feature totals
    total_chats = (await db.execute(
        select(func.count(distinct(Conversation.session_id)))
    )).scalar_one() or 0
    total_videos = (await db.execute(
        select(func.count()).select_from(ScoutingVideo)
    )).scalar_one() or 0
    total_plays = (await db.execute(
        select(func.count()).select_from(Play)
    )).scalar_one() or 0
    total_uploads = (await db.execute(
        select(func.count()).select_from(Upload)
    )).scalar_one() or 0

    # Agent usage — counts of user-role messages grouped by agent_used
    agent_usage = [
        {"agent_used": au, "cnt": int(c or 0)}
        for au, c in (await db.execute(
            select(Conversation.agent_used, func.count().label("cnt"))
            .where(Conversation.role == "user")
            .group_by(Conversation.agent_used)
            .order_by(func.count().desc())
        )).all()
    ]

    # Daily message volume (last 30 days)
    month_ago = datetime.now(UTC) - timedelta(days=30)
    daily_messages = [
        {"day": str(d), "cnt": int(c or 0)}
        for d, c in (await db.execute(
            select(
                func.date(Conversation.created_at).label("day"),
                func.count().label("cnt"),
            )
            .where(
                Conversation.role == "user",
                Conversation.created_at >= month_ago,
            )
            .group_by(func.date(Conversation.created_at))
            .order_by(func.date(Conversation.created_at))
        )).all()
    ]

    # Adoption percentages (per-feature distinct users / total users)
    total_users = (await db.execute(
        select(func.count()).select_from(User).where(User.deleted_at.is_(None))
    )).scalar_one() or 0
    chat_users = (await db.execute(
        select(func.count(distinct(Conversation.user_id)))
    )).scalar_one() or 0
    video_users = (await db.execute(
        select(func.count(distinct(ScoutingVideo.user_id)))
    )).scalar_one() or 0
    play_users = (await db.execute(
        select(func.count(distinct(Play.user_id)))
    )).scalar_one() or 0
    upload_users = (await db.execute(
        select(func.count(distinct(Upload.user_id)))
    )).scalar_one() or 0
    adoption = {
        "chat":   round(chat_users / total_users * 100, 1) if total_users else 0,
        "video":  round(video_users / total_users * 100, 1) if total_users else 0,
        "play":   round(play_users / total_users * 100, 1) if total_users else 0,
        "upload": round(upload_users / total_users * 100, 1) if total_users else 0,
    }

    return _empty_render(
        request,
        template="admin/feature_usage.html",
        active_tab="feature_usage",
        extra={
            "total_chats": total_chats,
            "total_videos": total_videos,
            "total_plays": total_plays,
            "total_uploads": total_uploads,
            "agent_usage": agent_usage,
            "daily_messages": daily_messages,
            "adoption": adoption,
            "total_users": total_users,
        },
    )


@router.get("/admin/feedback", response_class=HTMLResponse)
async def admin_feedback_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Feedback dashboard: thumbs-up vs thumbs-down totals, satisfaction
    %, per-agent breakdown, worst-performing agent, and recent negatives.
    Ports v1's `backend/admin/routes.py:feedback`."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.coach import Feedback
    from src.models.users import User

    total = (await db.execute(
        select(func.count()).select_from(Feedback)
    )).scalar_one() or 0
    positive = (await db.execute(
        select(func.count()).select_from(Feedback).where(Feedback.rating == 1)
    )).scalar_one() or 0
    negative = (await db.execute(
        select(func.count()).select_from(Feedback).where(Feedback.rating == -1)
    )).scalar_one() or 0
    satisfaction = round(positive / total * 100, 1) if total else 0

    per_agent = [
        {
            "agent_key": ak,
            "total": int(t or 0),
            "positive": int(p or 0),
            "negative": int(n or 0),
        }
        for ak, t, p, n in (await db.execute(
            select(
                Feedback.agent_key,
                func.count().label("total"),
                func.sum(case((Feedback.rating == 1, 1), else_=0)).label("positive"),
                func.sum(case((Feedback.rating == -1, 1), else_=0)).label("negative"),
            )
            .group_by(Feedback.agent_key)
        )).all()
    ]

    # Worst-performing agent (lowest score = positive - negative / total)
    worst_agent = "N/A"
    worst_score = 999.0
    for a in per_agent:
        if a["total"] > 0:
            score = (a["positive"] - a["negative"]) / a["total"]
            if score < worst_score:
                worst_score = score
                worst_agent = a["agent_key"] or "unknown"

    # Recent negative feedback — JOIN to users for email/display_name
    recent_negative_rows = (await db.execute(
        select(Feedback, User.email, User.display_name)
        .outerjoin(User, Feedback.user_id == User.id)
        .where(Feedback.rating == -1)
        .order_by(Feedback.created_at.desc().nulls_last())
        .limit(20)
    )).all()
    recent_negative = []
    for fb, email, display_name in recent_negative_rows:
        recent_negative.append({
            "id": fb.id,
            "user_id": fb.user_id,
            "agent_key": fb.agent_key,
            "message_content": fb.message_content,
            "response_content": fb.response_content,
            "rating": fb.rating,
            "comment": fb.comment,
            "created_at": fb.created_at,
            "email": email,
            "display_name": display_name,
        })

    return _empty_render(
        request,
        template="admin/feedback.html",
        active_tab="feedback",
        extra={
            "total": total,
            "positive": positive,
            "negative": negative,
            "satisfaction": satisfaction,
            "per_agent": per_agent,
            "worst_agent": worst_agent,
            "recent_negative": recent_negative,
        },
    )


@router.get("/admin/geography", response_class=HTMLResponse)
async def admin_geography_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Geographic distribution of users — joins audit_logs (most recent
    IP per user) to ip_geo_cache (country mapping). Ports v1's
    `backend/admin/routes.py:geography` minus the external ip-api.com
    resolution step (cache-only — uncached IPs are simply skipped).

    To populate the cache, a separate background job would need to call
    ip-api.com for unresolved IPs. Out of scope for this fix."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.auth import AuditLog
    from src.models.cache import IpGeoCache

    # Subquery: most-recent IP per user from audit_logs.
    user_ip_subq = (
        select(
            AuditLog.user_id,
            AuditLog.ip_address,
            func.max(AuditLog.created_at).label("latest"),
        )
        .where(
            AuditLog.user_id.is_not(None),
            AuditLog.ip_address.is_not(None),
            AuditLog.ip_address != "",
            AuditLog.ip_address != "127.0.0.1",
        )
        .group_by(AuditLog.user_id, AuditLog.ip_address)
        .subquery()
    )

    # Join to cache to resolve country. Skip rows with no cache hit.
    rows = (await db.execute(
        select(
            user_ip_subq.c.user_id,
            IpGeoCache.country_code,
            IpGeoCache.country_name,
            IpGeoCache.lat,
            IpGeoCache.lon,
        )
        .join(IpGeoCache, IpGeoCache.ip == user_ip_subq.c.ip_address)
        .where(
            IpGeoCache.country_name.is_not(None),
            IpGeoCache.country_name != "",
        )
    )).all()

    # Aggregate by country.
    country_counts: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = r.country_name
        if name not in country_counts:
            country_counts[name] = {
                "code": r.country_code,
                "count": 0,
                "lat": r.lat,
                "lon": r.lon,
            }
        country_counts[name]["count"] += 1

    sorted_countries = sorted(
        country_counts.items(),
        key=lambda kv: kv[1]["count"],
        reverse=True,
    )
    total_geolocated = len(rows)
    total_countries = len(country_counts)
    top_country = sorted_countries[0][0] if sorted_countries else "N/A"
    top_country_count = sorted_countries[0][1]["count"] if sorted_countries else 0
    country_data_json = json.dumps([
        {"name": n, "code": d["code"], "count": d["count"], "lat": d["lat"], "lon": d["lon"]}
        for n, d in sorted_countries
    ])

    return _empty_render(
        request,
        template="admin/geography.html",
        active_tab="geography",
        extra={
            "total_geolocated": total_geolocated,
            "total_countries": total_countries,
            "top_country": top_country,
            "top_country_count": top_country_count,
            "countries": sorted_countries,
            "country_data_json": country_data_json,
        },
    )


@router.get("/admin/user-activity", response_class=HTMLResponse)
async def admin_user_activity_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Engagement dashboard: total tracked time, active-today, average
    session, section heatmap, per-user totals, and optional per-user
    drill-down via `?user_id=N`. Ports v1's
    `backend/admin/routes.py:user_activity`.

    PageView.created_at is TEXT (legacy v1 schema) — we compare with an
    ISO string (space separator matches what `func.now()` writes)."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.analytics import PageView
    from src.models.users import User

    now = datetime.now(UTC)
    day_ago_iso = (now - timedelta(days=1)).isoformat(sep=" ")

    # Total tracked time (all users, seconds)
    total_time = int((await db.execute(
        select(func.coalesce(func.sum(PageView.duration_seconds), 0))
    )).scalar_one() or 0)

    # Active users today
    active_today = (await db.execute(
        select(func.count(distinct(PageView.user_id)))
        .where(PageView.created_at >= day_ago_iso)
    )).scalar_one() or 0

    # Average session duration (seconds) — subquery: per-session totals → AVG
    session_totals = (
        select(func.sum(PageView.duration_seconds).label("session_total"))
        .group_by(PageView.session_id)
        .subquery()
    )
    avg_session = float((await db.execute(
        select(func.coalesce(func.avg(session_totals.c.session_total), 0))
    )).scalar_one() or 0)

    # Section heatmap
    sections = [
        {
            "page_section": ps,
            "views": int(v or 0),
            "total_seconds": int(ts or 0),
            "unique_users": int(uu or 0),
        }
        for ps, v, ts, uu in (await db.execute(
            select(
                PageView.page_section,
                func.count().label("views"),
                func.sum(PageView.duration_seconds).label("total_seconds"),
                func.count(distinct(PageView.user_id)).label("unique_users"),
            )
            .group_by(PageView.page_section)
            .order_by(func.sum(PageView.duration_seconds).desc())
        )).all()
    ]
    top_section = sections[0]["page_section"] if sections else "N/A"

    # Per-user summary (HAVING > 0)
    user_list = [
        {
            "id": uid,
            "email": email,
            "display_name": display_name,
            "total_seconds": int(ts or 0),
            "last_active": last_active,
            "session_count": int(sc or 0),
        }
        for uid, email, display_name, ts, last_active, sc in (await db.execute(
            select(
                User.id,
                User.email,
                User.display_name,
                func.coalesce(func.sum(PageView.duration_seconds), 0).label("total_seconds"),
                func.max(PageView.created_at).label("last_active"),
                func.count(distinct(PageView.session_id)).label("session_count"),
            )
            .join(PageView, PageView.user_id == User.id)
            .where(User.deleted_at.is_(None))
            .group_by(User.id, User.email, User.display_name)
            .having(func.coalesce(func.sum(PageView.duration_seconds), 0) > 0)
            .order_by(func.sum(PageView.duration_seconds).desc())
        )).all()
    ]

    # Per-user drill-down (if ?user_id=N)
    user_detail = None
    detail_user = None
    detail_uid_raw = request.query_params.get("user_id")
    if detail_uid_raw and detail_uid_raw.isdigit():
        detail_uid = int(detail_uid_raw)
        du = (await db.execute(
            select(User.id, User.email, User.display_name)
            .where(User.id == detail_uid)
        )).first()
        if du is not None:
            detail_user = {
                "id": du.id,
                "email": du.email,
                "display_name": du.display_name,
            }
            user_detail = [
                {"page_section": ps, "views": int(v or 0), "total_seconds": int(ts or 0)}
                for ps, v, ts in (await db.execute(
                    select(
                        PageView.page_section,
                        func.count().label("views"),
                        func.sum(PageView.duration_seconds).label("total_seconds"),
                    )
                    .where(PageView.user_id == detail_uid)
                    .group_by(PageView.page_section)
                    .order_by(func.sum(PageView.duration_seconds).desc())
                )).all()
            ]

    return _empty_render(
        request,
        template="admin/user_activity.html",
        active_tab="user_activity",
        extra={
            "has_table": True,
            "users": user_list,
            "sections": sections,
            "user_detail": user_detail,
            "detail_user": detail_user,
            "total_time": total_time,
            "active_today": active_today,
            "avg_session": round(avg_session),
            "top_section": top_section,
        },
    )


@router.get("/admin/sales-inquiries", response_class=HTMLResponse)
async def admin_sales_inquiries_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Sales inquiries dashboard: total / new / this-week counts and
    per-plan breakdown (academy + legacy academy10, enterprise).
    Ports v1's `backend/admin/routes.py:sales_inquiries`.

    SalesInquiry.created_at is TEXT — use ISO-string compare for the
    this-week window (same trick as user-activity)."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.inquiries import SalesInquiry

    week_ago_iso = (datetime.now(UTC) - timedelta(days=7)).isoformat(sep=" ")

    total = (await db.execute(
        select(func.count()).select_from(SalesInquiry)
    )).scalar_one() or 0
    new_count = (await db.execute(
        select(func.count()).select_from(SalesInquiry)
        .where(SalesInquiry.status == "new")
    )).scalar_one() or 0
    this_week = (await db.execute(
        select(func.count()).select_from(SalesInquiry)
        .where(SalesInquiry.created_at >= week_ago_iso)
    )).scalar_one() or 0
    # 'academy10' is a legacy plan code carried over from v1.
    academy_count = (await db.execute(
        select(func.count()).select_from(SalesInquiry)
        .where(SalesInquiry.plan.in_(["academy", "academy10"]))
    )).scalar_one() or 0
    enterprise_count = (await db.execute(
        select(func.count()).select_from(SalesInquiry)
        .where(SalesInquiry.plan == "enterprise")
    )).scalar_one() or 0

    inquiries = list((await db.execute(
        select(SalesInquiry).order_by(SalesInquiry.created_at.desc().nulls_last())
    )).scalars().all())

    return _empty_render(
        request,
        template="admin/sales_inquiries.html",
        active_tab="sales",
        extra={
            "total": total,
            "new_count": new_count,
            "this_week": this_week,
            "academy_count": academy_count,
            "enterprise_count": enterprise_count,
            "inquiries": inquiries,
        },
    )


@router.get("/admin/research-sources", response_class=HTMLResponse)
async def admin_research_sources_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Observability over the open-search research tool: which tier-4
    (unknown) domains produced findings (promotion candidates) vs which
    high-volume domains keep returning zero (blacklist candidates).
    Ports v1's `backend/admin/routes.py:research_sources`.

    `research_url_log` has no writer yet (same as v1) — the page will
    render with zeros until the research pipeline starts logging."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.analytics import ResearchUrlLog

    month_ago = datetime.now(UTC) - timedelta(days=30)

    total_extracts = (await db.execute(
        select(func.count()).select_from(ResearchUrlLog)
        .where(ResearchUrlLog.used_at >= month_ago)
    )).scalar_one() or 0

    unique_domains = (await db.execute(
        select(func.count(distinct(ResearchUrlLog.domain)))
        .where(ResearchUrlLog.used_at >= month_ago)
    )).scalar_one() or 0

    # Tier-4 (unknown) domains that produced findings → promote
    promote_candidates = [
        {
            "domain": d,
            "uses": int(u or 0),
            "total_findings": int(tf or 0),
            "avg_findings": float(af or 0),
            "last_used": lu,
        }
        for d, u, tf, af, lu in (await db.execute(
            select(
                ResearchUrlLog.domain,
                func.count().label("uses"),
                func.sum(ResearchUrlLog.findings_count).label("total_findings"),
                func.avg(ResearchUrlLog.findings_count).label("avg_findings"),
                func.max(ResearchUrlLog.used_at).label("last_used"),
            )
            .where(
                ResearchUrlLog.tier == 4,
                ResearchUrlLog.used_at >= month_ago,
            )
            .group_by(ResearchUrlLog.domain)
            .having(func.sum(ResearchUrlLog.findings_count) > 0)
            .order_by(
                func.sum(ResearchUrlLog.findings_count).desc(),
                func.count().desc(),
            )
            .limit(30)
        )).all()
    ]

    # Any-tier domains with ≥3 uses and zero findings → blacklist
    blacklist_candidates = [
        {
            "domain": d,
            "uses": int(u or 0),
            "tier": int(t or 0),
            "last_used": lu,
        }
        for d, u, t, lu in (await db.execute(
            select(
                ResearchUrlLog.domain,
                func.count().label("uses"),
                func.max(ResearchUrlLog.tier).label("tier"),
                func.max(ResearchUrlLog.used_at).label("last_used"),
            )
            .where(ResearchUrlLog.used_at >= month_ago)
            .group_by(ResearchUrlLog.domain)
            .having(
                func.sum(ResearchUrlLog.findings_count) == 0,
                func.count() >= 3,
            )
            .order_by(func.count().desc())
            .limit(30)
        )).all()
    ]

    by_tier = [
        {"tier": int(t or 0), "uses": int(u or 0), "total_findings": int(tf or 0)}
        for t, u, tf in (await db.execute(
            select(
                ResearchUrlLog.tier,
                func.count().label("uses"),
                func.sum(ResearchUrlLog.findings_count).label("total_findings"),
            )
            .where(ResearchUrlLog.used_at >= month_ago)
            .group_by(ResearchUrlLog.tier)
            .order_by(ResearchUrlLog.tier)
        )).all()
    ]

    recent = [
        {
            "url": r.url,
            "domain": r.domain,
            "tier": r.tier,
            "findings_count": r.findings_count,
            "used_at": r.used_at,
        }
        for r in (await db.execute(
            select(ResearchUrlLog)
            .order_by(ResearchUrlLog.used_at.desc().nulls_last())
            .limit(50)
        )).scalars().all()
    ]

    return _empty_render(
        request,
        template="admin/research_sources.html",
        active_tab="research_sources",
        extra={
            "has_table": True,
            "rows_30d": total_extracts,
            "total_extracts": total_extracts,
            "unique_domains": unique_domains,
            "promote_candidates": promote_candidates,
            "blacklist_candidates": blacklist_candidates,
            "by_tier": by_tier,
            "recent": recent,
        },
    )


@router.get("/admin/email-log", response_class=HTMLResponse)
async def admin_email_log_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Email log dashboard: window counts (24h/7d/30d), failure count,
    template breakdown, and filterable row list. Ports v1's
    `backend/admin/email_routes.py:email_log_view`."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.email import EmailLog

    now = datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # Stat windows — EmailLog.sent_at + .created_at are DateTime so we
    # can compare against Python datetime directly.
    total_24h = (await db.execute(
        select(func.count()).select_from(EmailLog)
        .where(EmailLog.sent_at >= day_ago)
    )).scalar_one() or 0
    total_7d = (await db.execute(
        select(func.count()).select_from(EmailLog)
        .where(EmailLog.sent_at >= week_ago)
    )).scalar_one() or 0
    total_30d = (await db.execute(
        select(func.count()).select_from(EmailLog)
        .where(EmailLog.sent_at >= month_ago)
    )).scalar_one() or 0
    failed_30d = (await db.execute(
        select(func.count()).select_from(EmailLog)
        .where(EmailLog.status == "failed", EmailLog.created_at >= month_ago)
    )).scalar_one() or 0

    # Template breakdown over last 30 days
    by_template_rows = (await db.execute(
        select(EmailLog.template, func.count().label("cnt"))
        .where(EmailLog.created_at >= month_ago)
        .group_by(EmailLog.template)
        .order_by(func.count().desc())
    )).all()
    by_template = [{"template": t, "cnt": c} for t, c in by_template_rows]

    # Filters from query string (Flask used request.args; FastAPI = query_params)
    filter_template = (request.query_params.get("template") or "").strip()
    filter_status = (request.query_params.get("status") or "").strip()
    filter_email = (request.query_params.get("email") or "").strip().lower()

    stmt = select(EmailLog)
    if filter_template:
        stmt = stmt.where(EmailLog.template == filter_template)
    if filter_status:
        stmt = stmt.where(EmailLog.status == filter_status)
    if filter_email:
        stmt = stmt.where(func.lower(EmailLog.to_email).like(f"%{filter_email}%"))
    stmt = stmt.order_by(EmailLog.id.desc()).limit(200)
    rows = list((await db.execute(stmt)).scalars().all())

    # Distinct values for the filter dropdowns
    template_names = [t for (t,) in (await db.execute(
        select(EmailLog.template).distinct().order_by(EmailLog.template)
    )).all()]

    return _empty_render(
        request,
        template="admin/email_log.html",
        active_tab="emails",
        extra={
            "total_24h": total_24h,
            "total_7d": total_7d,
            "total_30d": total_30d,
            "failed_30d": failed_30d,
            "by_template": by_template,
            "rows": rows,
            "templates": template_names,
            "filter_template": filter_template,
            "filter_status": filter_status,
            "filter_email": filter_email,
        },
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
async def admin_email_compose_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Email composer landing: shows mailing-list dropdown + total
    reachable user count + admin email for the default From.
    Ports v1's `backend/admin/email_routes.py:email_compose_view`."""
    guard = _require_admin(request)
    if isinstance(guard, RedirectResponse):
        return guard

    from src.models.email import MailingList
    from src.models.users import User

    lists = [
        {"id": ml.id, "name": ml.name}
        for ml in (await db.execute(
            select(MailingList).order_by(MailingList.name)
        )).scalars().all()
    ]
    user_count = (await db.execute(
        select(func.count()).select_from(User)
        .where(User.deleted_at.is_(None), User.email.is_not(None))
    )).scalar_one() or 0

    return _empty_render(
        request,
        template="admin/email_compose.html",
        active_tab="emails",
        extra={
            "lists": lists,
            "user_count": user_count,
            "admin_email": settings.ADMIN_EMAIL,
        },
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
