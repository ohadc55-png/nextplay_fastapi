"""Admin panel — auth + dashboard data endpoints.

Async port of the read-only / aggregation pieces of `backend/admin/routes.py`.
The HTML page rendering is deferred to Phase 8 (Jinja2 templates); this
router exposes JSON-API endpoints that the templates' vanilla JS will hit.

Structure mirrors v1's tabs:
  /admin/login, /admin/logout, /admin/me            — auth
  /admin/api/stats                                  — header counts
  /admin/api/dashboard                              — landing-page summary
  /admin/api/dashboard/growth                       — 30-day signup chart
  /admin/api/users                                  — Users tab
  /admin/api/api-costs                              — Costs tab
  /admin/api/feature-usage                          — Feature Usage tab
  /admin/api/feedback                               — Feedback tab
  /admin/api/geography                              — Geography tab (cache-only)
  /admin/api/user-activity                          — Activity tab
  /admin/api/sales-inquiries                        — Sales tab
  /admin/api/research-sources                       — Research Sources tab
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import ADMIN_SESSION_KEY, get_current_admin
from src.auth.admin_auth import (
    is_admin_auth_configured,
    is_admin_email,
    verify_admin_password,
)
from src.core.database import get_db
from src.frontend import page_context, templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Auth — login / logout / me
# ---------------------------------------------------------------------------

class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str


def _render_login_with_error(request: Request, error: str, status_code: int = 401):
    """Re-render the login template with an inline error so the browser
    keeps the URL on /admin/login (instead of dumping a JSON response)."""
    ctx = page_context(request, user=None, extra={
        "active_tab": "login",
        "admin_email": None,
        "error": error,
    })
    return templates.TemplateResponse(
        "admin/login.html", ctx, status_code=status_code,
    )


def _wants_html(request: Request) -> bool:
    """True when the caller looks like a browser submitting a form (no
    JSON body, no `Accept: application/json` from a fetch). Used to
    pick between 303→/admin/dashboard and a JSON 200 response."""
    ct = (request.headers.get("content-type") or "").lower()
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in ct:
        return False
    if "application/json" in accept and "text/html" not in accept:
        return False
    return True


@router.post("/login")
async def admin_login(request: Request):
    """Log in as admin. Validates: (1) email is in ADMIN_EMAILS, (2)
    ADMIN_PASSWORD_HASH is configured (else fail-closed), (3) bcrypt
    password match. On success writes `admin_email` to the Starlette
    session — subsequent requests carry the signed session cookie.

    Dual-shape:
      - HTML form submit (Content-Type: application/x-www-form-urlencoded
        or multipart/form-data) → 303 redirect to /admin/dashboard on
        success, 401 with re-rendered template on failure.
      - JSON request → 200 {"ok": true, "email": ...} on success, 401
        JSON on failure. Preserves the contract used by tests + any
        future API client."""
    # Read whichever shape the caller sent.
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        try:
            body = await request.json()
        except Exception:
            body = {}
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
    else:
        form = await request.form()
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""

    html_response = _wants_html(request)

    if not is_admin_auth_configured():
        if html_response:
            return _render_login_with_error(
                request,
                "Admin auth not configured (ADMIN_PASSWORD_HASH unset)",
                status_code=503,
            )
        raise HTTPException(
            status_code=503,
            detail="Admin auth not configured (ADMIN_PASSWORD_HASH unset)",
        )

    normalized = email.lower()
    if not is_admin_email(normalized) or not verify_admin_password(password):
        if html_response:
            return _render_login_with_error(request, "Invalid email or password")
        raise HTTPException(status_code=401, detail="Invalid email or password")

    request.session[ADMIN_SESSION_KEY] = normalized
    if html_response:
        # 303 → forces the browser to GET /admin/dashboard (POST→GET handoff)
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    return {"ok": True, "email": normalized}


@router.post("/logout")
async def admin_logout(request: Request) -> dict:
    """Clear the admin session. Idempotent (calling when not logged in is OK)."""
    request.session.pop(ADMIN_SESSION_KEY, None)
    return {"ok": True}


@router.get("/me")
async def admin_me(email: str = Depends(get_current_admin)) -> dict:
    """Return the currently-authenticated admin email. Used by the panel
    JS to detect 'are we still logged in?'"""
    return {"email": email}


# ---------------------------------------------------------------------------
# Dashboard primitives
# ---------------------------------------------------------------------------

async def _scalar(db: AsyncSession, sql: str, **params: Any) -> Any:
    """Tiny wrapper that returns the first column of the first row. Used
    everywhere in v1 via `query_scalar`."""
    return (await db.execute(text(sql), params)).scalar()


async def _rows(db: AsyncSession, sql: str, **params: Any) -> list[dict]:
    """Map result rows to dicts so the JSON serializer can ship them.
    Mirrors `query_db` from v1."""
    result = await db.execute(text(sql), params)
    return [dict(r) for r in result.mappings().all()]


def _pct(num: int, denom: int) -> float:
    return round(num / denom * 100, 1) if denom else 0.0


# ---------------------------------------------------------------------------
# /api/stats — header counts
# ---------------------------------------------------------------------------

@router.get("/api/stats")
async def admin_stats(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    total = await _scalar(db, "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL")
    active = await _scalar(
        db,
        "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL "
        "AND last_login_at >= :cutoff",
        cutoff=(date.today() - timedelta(days=7)).isoformat(),
    )
    return {"total_users": int(total or 0), "active_7d": int(active or 0)}


# ---------------------------------------------------------------------------
# /api/dashboard — landing summary
# ---------------------------------------------------------------------------

TASK_STATUSES = ["backlog", "in_progress", "done"]
TASK_PRIORITIES = ["critical", "high", "medium", "low"]
TASK_TYPES = ["feature", "bug", "refactor", "ops", "design", "docs", "research", "other"]
TASK_TAGS = [
    "frontend", "backend", "bug", "UX", "infra", "docs",
    "research", "video", "AI", "scouting", "devops",
]


@router.get("/api/dashboard")
async def admin_dashboard(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    month_ago = (today - timedelta(days=30)).isoformat()

    total_users = await _scalar(db, "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL")
    new_7d = await _scalar(
        db,
        "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL AND created_at >= :c",
        c=week_ago,
    )
    new_30d = await _scalar(
        db,
        "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL AND created_at >= :c",
        c=month_ago,
    )

    task_counts: dict[str, int] = {}
    for p in TASK_PRIORITIES:
        n = await _scalar(
            db,
            "SELECT COUNT(*) FROM admin_tasks WHERE status != 'done' AND priority = :p",
            p=p,
        )
        task_counts[p] = int(n or 0)
    open_total = sum(task_counts.values())

    overdue = await _scalar(
        db,
        "SELECT COUNT(*) FROM admin_tasks "
        "WHERE status != 'done' AND due_date IS NOT NULL AND due_date < :today",
        today=today.isoformat(),
    )

    return {
        "total_users": int(total_users or 0),
        "new_7d": int(new_7d or 0),
        "new_30d": int(new_30d or 0),
        "task_counts": task_counts,
        "open_total": open_total,
        "overdue": int(overdue or 0),
        "task_statuses": TASK_STATUSES,
        "task_priorities": TASK_PRIORITIES,
        "task_types": TASK_TYPES,
        "task_tags": TASK_TAGS,
    }


@router.get("/api/dashboard/growth")
async def admin_dashboard_growth(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """30-day daily-signup series. Always returns a complete 30-element
    series so the chart can render zeros on quiet days."""
    rows = await _rows(
        db,
        "SELECT DATE(created_at) AS day, COUNT(*) AS cnt "
        "FROM users WHERE deleted_at IS NULL AND created_at >= :c "
        "GROUP BY DATE(created_at) ORDER BY day ASC",
        c=(date.today() - timedelta(days=30)).isoformat(),
    )
    by_day = {str(r["day"]): int(r["cnt"]) for r in rows}
    today = date.today()
    series = [
        {"day": (today - timedelta(days=i)).isoformat(),
         "cnt": by_day.get((today - timedelta(days=i)).isoformat(), 0)}
        for i in range(29, -1, -1)
    ]
    return {"series": series}


# ---------------------------------------------------------------------------
# Users tab
# ---------------------------------------------------------------------------

@router.get("/api/users")
async def admin_users(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    today = date.today()
    yday = (today - timedelta(days=1)).isoformat()
    week = (today - timedelta(days=7)).isoformat()

    total = int(await _scalar(db, "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL") or 0)
    new_today = int(await _scalar(
        db, "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL AND created_at >= :c", c=yday) or 0)
    new_week = int(await _scalar(
        db, "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL AND created_at >= :c", c=week) or 0)
    active_7d = int(await _scalar(
        db, "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL AND last_login_at >= :c", c=week) or 0)

    plans = await _rows(
        db,
        "SELECT subscription_plan, COUNT(*) AS cnt FROM users "
        "WHERE deleted_at IS NULL GROUP BY subscription_plan",
    )
    plan_map = {(p["subscription_plan"] or "none"): int(p["cnt"]) for p in plans}

    user_list = await _rows(
        db,
        "SELECT u.id, u.email, u.display_name, u.subscription_plan,"
        " u.trial_ends_at, u.last_login_at, u.created_at,"
        " (SELECT COUNT(DISTINCT session_id) FROM conversations WHERE user_id = u.id) AS chat_count,"
        " (SELECT COUNT(*) FROM scouting_videos WHERE user_id = u.id) AS video_count,"
        " (SELECT COUNT(*) FROM plays WHERE user_id = u.id) AS play_count,"
        " (SELECT COUNT(*) FROM team_profile WHERE user_id = u.id) AS team_count"
        " FROM users u WHERE u.deleted_at IS NULL ORDER BY u.created_at DESC",
    )

    return {
        "total": total,
        "new_today": new_today,
        "new_week": new_week,
        "active_7d": active_7d,
        "plan_map": plan_map,
        "users": user_list,
    }


# ---------------------------------------------------------------------------
# API costs tab
# ---------------------------------------------------------------------------

@router.get("/api/api-costs")
async def admin_api_costs(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    today = date.today()
    yday = (today - timedelta(days=1)).isoformat()
    week = (today - timedelta(days=7)).isoformat()
    month = (today - timedelta(days=30)).isoformat()

    cost_today = float(await _scalar(
        db, "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage_logs WHERE created_at >= :c", c=yday) or 0)
    cost_week = float(await _scalar(
        db, "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage_logs WHERE created_at >= :c", c=week) or 0)
    cost_month = float(await _scalar(
        db, "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage_logs WHERE created_at >= :c", c=month) or 0)
    tokens_today = int(await _scalar(
        db, "SELECT COALESCE(SUM(total_tokens), 0) FROM api_usage_logs WHERE created_at >= :c", c=yday) or 0)

    daily_costs = await _rows(
        db,
        "SELECT DATE(created_at) AS day, SUM(cost_usd) AS cost "
        "FROM api_usage_logs WHERE created_at >= :c "
        "GROUP BY DATE(created_at) ORDER BY day",
        c=month,
    )
    by_model = await _rows(
        db,
        "SELECT model, SUM(cost_usd) AS cost, SUM(total_tokens) AS tokens "
        "FROM api_usage_logs GROUP BY model ORDER BY cost DESC",
    )
    by_agent = await _rows(
        db,
        "SELECT agent_key, SUM(cost_usd) AS cost, COUNT(*) AS calls "
        "FROM api_usage_logs WHERE agent_key IS NOT NULL "
        "GROUP BY agent_key ORDER BY cost DESC",
    )
    top_users = await _rows(
        db,
        "SELECT u.email, u.display_name,"
        " SUM(a.cost_usd) AS total_cost, SUM(a.total_tokens) AS total_tokens "
        "FROM api_usage_logs a JOIN users u ON a.user_id = u.id "
        "GROUP BY a.user_id, u.email, u.display_name "
        "ORDER BY total_cost DESC LIMIT 10",
    )

    avg_daily_7d = float(await _scalar(
        db,
        "SELECT COALESCE(SUM(cost_usd), 0) / 7.0 FROM api_usage_logs WHERE created_at >= :c",
        c=week,
    ) or 0)
    monthly_projection = round(avg_daily_7d * 30, 2)

    total_sessions_30d = int(await _scalar(
        db,
        "SELECT COUNT(DISTINCT session_id) FROM conversations WHERE created_at >= :c",
        c=month,
    ) or 0)
    cost_per_session = round(cost_month / total_sessions_30d, 4) if total_sessions_30d else 0

    return {
        "cost_today": cost_today,
        "cost_week": cost_week,
        "cost_month": cost_month,
        "tokens_today": tokens_today,
        "daily_costs": daily_costs,
        "by_model": by_model,
        "by_agent": by_agent,
        "top_users": top_users,
        "monthly_projection": monthly_projection,
        "cost_per_session": cost_per_session,
    }


# ---------------------------------------------------------------------------
# Feature Usage tab
# ---------------------------------------------------------------------------

@router.get("/api/feature-usage")
async def admin_feature_usage(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    total_chats = int(await _scalar(db, "SELECT COUNT(DISTINCT session_id) FROM conversations") or 0)
    total_videos = int(await _scalar(db, "SELECT COUNT(*) FROM scouting_videos") or 0)
    total_plays = int(await _scalar(db, "SELECT COUNT(*) FROM plays") or 0)
    total_uploads = int(await _scalar(db, "SELECT COUNT(*) FROM uploads") or 0)
    total_users = int(await _scalar(db, "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL") or 0)

    chat_users = int(await _scalar(db, "SELECT COUNT(DISTINCT user_id) FROM conversations") or 0)
    video_users = int(await _scalar(db, "SELECT COUNT(DISTINCT user_id) FROM scouting_videos") or 0)
    play_users = int(await _scalar(db, "SELECT COUNT(DISTINCT user_id) FROM plays") or 0)
    upload_users = int(await _scalar(db, "SELECT COUNT(DISTINCT user_id) FROM uploads") or 0)

    agent_usage = await _rows(
        db,
        "SELECT agent_used, COUNT(*) AS cnt FROM conversations "
        "WHERE role='user' GROUP BY agent_used ORDER BY cnt DESC",
    )
    daily_messages = await _rows(
        db,
        "SELECT DATE(created_at) AS day, COUNT(*) AS cnt FROM conversations "
        "WHERE role='user' AND created_at >= :c "
        "GROUP BY DATE(created_at) ORDER BY day",
        c=(date.today() - timedelta(days=30)).isoformat(),
    )

    return {
        "total_chats": total_chats,
        "total_videos": total_videos,
        "total_plays": total_plays,
        "total_uploads": total_uploads,
        "total_users": total_users,
        "agent_usage": agent_usage,
        "daily_messages": daily_messages,
        "adoption": {
            "chat": _pct(chat_users, total_users),
            "video": _pct(video_users, total_users),
            "play": _pct(play_users, total_users),
            "upload": _pct(upload_users, total_users),
        },
    }


# ---------------------------------------------------------------------------
# Feedback tab
# ---------------------------------------------------------------------------

@router.get("/api/feedback")
async def admin_feedback_summary(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    total = int(await _scalar(db, "SELECT COUNT(*) FROM feedback") or 0)
    positive = int(await _scalar(db, "SELECT COUNT(*) FROM feedback WHERE rating = 1") or 0)
    negative = int(await _scalar(db, "SELECT COUNT(*) FROM feedback WHERE rating = -1") or 0)
    satisfaction = _pct(positive, total)

    per_agent = await _rows(
        db,
        "SELECT agent_key, COUNT(*) AS total,"
        " SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS positive,"
        " SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) AS negative "
        "FROM feedback GROUP BY agent_key",
    )

    worst_agent, worst_score = "N/A", 999.0
    for a in per_agent:
        if (a["total"] or 0) > 0:
            score = ((a["positive"] or 0) - (a["negative"] or 0)) / a["total"]
            if score < worst_score:
                worst_score, worst_agent = score, (a["agent_key"] or "unknown")

    recent_negative = await _rows(
        db,
        "SELECT f.id, f.user_id, f.team_id, f.agent_key, f.rating,"
        " f.message_content, f.response_content, f.comment, f.created_at,"
        " u.email, u.display_name "
        "FROM feedback f LEFT JOIN users u ON f.user_id = u.id "
        "WHERE f.rating = -1 ORDER BY f.created_at DESC LIMIT 20",
    )

    return {
        "total": total,
        "positive": positive,
        "negative": negative,
        "satisfaction": satisfaction,
        "per_agent": per_agent,
        "worst_agent": worst_agent,
        "recent_negative": recent_negative,
    }


# ---------------------------------------------------------------------------
# Geography tab — cache-only (no IP API calls; that's a Phase 7 background job)
# ---------------------------------------------------------------------------

@router.get("/api/geography")
async def admin_geography(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Read-only summary of cached IP geo data. The IP-resolution job that
    populates ip_geo_cache from audit_logs.ip_address is a background
    task scheduled in Phase 7 — this endpoint just renders what's
    already cached."""
    rows = await _rows(
        db,
        "SELECT a.user_id, g.country_code, g.country_name, g.lat, g.lon "
        "FROM (SELECT user_id, ip_address, MAX(created_at) AS latest"
        "      FROM audit_logs WHERE user_id IS NOT NULL AND ip_address IS NOT NULL"
        "        AND ip_address != '127.0.0.1' "
        "      GROUP BY user_id, ip_address) a "
        "JOIN ip_geo_cache g ON g.ip = a.ip_address "
        "WHERE g.country_name IS NOT NULL AND g.country_name != ''",
    )

    country_counts: dict[str, dict] = {}
    for row in rows:
        name = row["country_name"]
        if name not in country_counts:
            country_counts[name] = {
                "code": row["country_code"], "count": 0,
                "lat": row["lat"], "lon": row["lon"],
            }
        country_counts[name]["count"] += 1

    sorted_countries = sorted(
        country_counts.items(), key=lambda x: x[1]["count"], reverse=True
    )
    total_geolocated = len(rows)
    total_countries = len(country_counts)
    top_country = sorted_countries[0][0] if sorted_countries else "N/A"
    top_country_count = sorted_countries[0][1]["count"] if sorted_countries else 0

    return {
        "total_geolocated": total_geolocated,
        "total_countries": total_countries,
        "top_country": top_country,
        "top_country_count": top_country_count,
        "countries": [
            {"name": n, "code": d["code"], "count": d["count"],
             "lat": d["lat"], "lon": d["lon"]}
            for n, d in sorted_countries
        ],
    }


# ---------------------------------------------------------------------------
# User Activity tab
# ---------------------------------------------------------------------------

@router.get("/api/user-activity")
async def admin_user_activity(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    user_id: int | None = None,
) -> dict:
    yday = (date.today() - timedelta(days=1)).isoformat()

    total_time = int(await _scalar(
        db, "SELECT COALESCE(SUM(duration_seconds), 0) FROM page_views") or 0)
    active_today = int(await _scalar(
        db, "SELECT COUNT(DISTINCT user_id) FROM page_views WHERE created_at >= :c", c=yday) or 0)
    avg_session = float(await _scalar(
        db,
        "SELECT COALESCE(AVG(session_total), 0) FROM "
        "(SELECT session_id, SUM(duration_seconds) AS session_total "
        " FROM page_views GROUP BY session_id) sub",
    ) or 0)

    sections = await _rows(
        db,
        "SELECT page_section, COUNT(*) AS views,"
        " SUM(duration_seconds) AS total_seconds,"
        " COUNT(DISTINCT user_id) AS unique_users "
        "FROM page_views GROUP BY page_section ORDER BY total_seconds DESC",
    )
    top_section = sections[0]["page_section"] if sections else "N/A"

    user_list = await _rows(
        db,
        "SELECT u.id, u.email, u.display_name,"
        " COALESCE(SUM(pv.duration_seconds), 0) AS total_seconds,"
        " MAX(pv.created_at) AS last_active,"
        " COUNT(DISTINCT pv.session_id) AS session_count "
        "FROM users u JOIN page_views pv ON pv.user_id = u.id "
        "WHERE u.deleted_at IS NULL "
        "GROUP BY u.id, u.email, u.display_name "
        "HAVING COALESCE(SUM(pv.duration_seconds), 0) > 0 "
        "ORDER BY total_seconds DESC",
    )

    user_detail: list[dict] | None = None
    detail_user: dict | None = None
    if user_id is not None:
        rows = await _rows(
            db, "SELECT id, email, display_name FROM users WHERE id = :u", u=user_id)
        detail_user = rows[0] if rows else None
        user_detail = await _rows(
            db,
            "SELECT page_section, COUNT(*) AS views, SUM(duration_seconds) AS total_seconds "
            "FROM page_views WHERE user_id = :u "
            "GROUP BY page_section ORDER BY total_seconds DESC",
            u=user_id,
        )

    return {
        "total_time": total_time,
        "active_today": active_today,
        "avg_session": round(avg_session),
        "top_section": top_section,
        "sections": sections,
        "users": user_list,
        "user_detail": user_detail,
        "detail_user": detail_user,
    }


# ---------------------------------------------------------------------------
# Sales Inquiries tab
# ---------------------------------------------------------------------------

@router.get("/api/sales-inquiries")
async def admin_sales_inquiries(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    week = (date.today() - timedelta(days=7)).isoformat()

    total = int(await _scalar(db, "SELECT COUNT(*) FROM sales_inquiries") or 0)
    new_count = int(await _scalar(
        db, "SELECT COUNT(*) FROM sales_inquiries WHERE status = 'new'") or 0)
    this_week = int(await _scalar(
        db, "SELECT COUNT(*) FROM sales_inquiries WHERE created_at >= :c", c=week) or 0)
    academy = int(await _scalar(
        db, "SELECT COUNT(*) FROM sales_inquiries WHERE plan IN ('academy', 'academy10')") or 0)
    enterprise = int(await _scalar(
        db, "SELECT COUNT(*) FROM sales_inquiries WHERE plan = 'enterprise'") or 0)
    inquiries = await _rows(
        db, "SELECT * FROM sales_inquiries ORDER BY created_at DESC")

    return {
        "total": total,
        "new_count": new_count,
        "this_week": this_week,
        "academy_count": academy,
        "enterprise_count": enterprise,
        "inquiries": inquiries,
    }


@router.patch("/api/sales-inquiries/{inquiry_id}/status")
async def admin_inquiry_status(
    inquiry_id: int,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    new_status: str = Body(..., embed=True),
) -> dict:
    """Move an inquiry through the funnel (new → contacted → converted/lost).
    Whitelist-validated to prevent typos becoming silent garbage statuses."""
    allowed = {"new", "contacted", "converted", "lost"}
    if new_status not in allowed:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(allowed)}")
    result = await db.execute(
        text("UPDATE sales_inquiries SET status = :s WHERE id = :i"),
        {"s": new_status, "i": inquiry_id},
    )
    await db.flush()
    if (result.rowcount or 0) == 0:
        raise HTTPException(status_code=404, detail="inquiry not found")
    return {"ok": True, "status": new_status}


# ---------------------------------------------------------------------------
# Research Sources tab — promotion / blacklist candidates
# ---------------------------------------------------------------------------

@router.get("/api/research-sources")
async def admin_research_sources(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    month = (date.today() - timedelta(days=30)).isoformat()

    total_extracts = int(await _scalar(
        db, "SELECT COUNT(*) FROM research_url_log WHERE used_at >= :c", c=month) or 0)
    unique_domains = int(await _scalar(
        db, "SELECT COUNT(DISTINCT domain) FROM research_url_log WHERE used_at >= :c",
        c=month) or 0)

    promote = await _rows(
        db,
        "SELECT domain, COUNT(*) AS uses,"
        " SUM(findings_count) AS total_findings,"
        " AVG(findings_count) AS avg_findings,"
        " MAX(used_at) AS last_used "
        "FROM research_url_log WHERE tier = 4 AND used_at >= :c "
        "GROUP BY domain HAVING SUM(findings_count) > 0 "
        "ORDER BY SUM(findings_count) DESC, COUNT(*) DESC LIMIT 30",
        c=month,
    )
    blacklist = await _rows(
        db,
        "SELECT domain, COUNT(*) AS uses, MAX(tier) AS tier, MAX(used_at) AS last_used "
        "FROM research_url_log WHERE used_at >= :c "
        "GROUP BY domain HAVING SUM(findings_count) = 0 AND COUNT(*) >= 3 "
        "ORDER BY COUNT(*) DESC LIMIT 30",
        c=month,
    )
    by_tier = await _rows(
        db,
        "SELECT tier, COUNT(*) AS uses, SUM(findings_count) AS total_findings "
        "FROM research_url_log WHERE used_at >= :c "
        "GROUP BY tier ORDER BY tier",
        c=month,
    )
    recent = await _rows(
        db,
        "SELECT url, domain, tier, findings_count, used_at "
        "FROM research_url_log ORDER BY used_at DESC LIMIT 50",
    )

    return {
        "total_extracts": total_extracts,
        "unique_domains": unique_domains,
        "promote_candidates": promote,
        "blacklist_candidates": blacklist,
        "by_tier": by_tier,
        "recent": recent,
    }
