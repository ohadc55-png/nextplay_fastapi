"""Frontend page routes — Jinja2 HTML responses.

Phase 8 batch 2. Async port of `frontend/routes.py`. Three groups:

  Anonymous pages — `/main`, `/privacy`, `/terms`, `/contact-sales`,
  `/login`, `/register` (the last two are stubs because the JSON
  auth flow lives in `/api/auth/*`; the HTML pages just render forms
  the SPA submits via fetch).

  Authed pages — `/`, `/chat`, `/team-setup`, `/data-upload`,
  `/history`, `/profile`, `/settings`, `/upgrade`, `/checkout/<plan>`,
  `/court-preview`, `/club-admin`. Each fetches the data v1 fetched
  (team profile, players, uploads, sessions, etc.) so the same
  templates render with the same context.

  Player profile — `/player/<id>` joins player + metrics + photo.

The routes use `get_current_user_optional` for anonymous flows and
`get_current_user` for gated ones. CSRF middleware exempts GET
requests automatically.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user, get_current_user_optional
from src.core.database import get_db
from src.frontend import page_context, templates
from src.models.users import User

logger = logging.getLogger(__name__)


router = APIRouter(tags=["pages"])


# ---------------------------------------------------------------------------
# Anonymous pages
# ---------------------------------------------------------------------------


def _oauth_flags() -> dict[str, bool]:
    """Return `{has_google, has_facebook, has_apple}` based on which
    OAuth providers have credentials configured. Templates check these
    flags to decide whether to render each social-login button."""
    from src.core.config import settings

    return {
        "has_google": bool((settings.GOOGLE_CLIENT_ID or "").strip()),
        "has_facebook": bool((settings.FACEBOOK_CLIENT_ID or "").strip()),
        "has_apple": bool((settings.APPLE_CLIENT_ID or "").strip()),
    }


@router.get("/main", response_class=HTMLResponse)
async def main_landing(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
) -> Any:
    return templates.TemplateResponse(
        "welcome.html",
        page_context(request, user=user, extra=_oauth_flags()),
    )


@router.get("/play-creator", response_class=HTMLResponse)
async def play_creator_landing(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
) -> Any:
    """SEO landing page targeting 'draw basketball plays' / 'play designer'
    queries. Top-of-funnel: pulls coaches in through a high-demand keyword,
    then funnels them to the full AI Coaching Staff trial signup."""
    return templates.TemplateResponse(
        "play_creator.html",
        page_context(request, user=user, extra=_oauth_flags()),
    )


# ---------------------------------------------------------------------------
# SEO surfaces — robots.txt + sitemap.xml
# ---------------------------------------------------------------------------


_SITE_URL = "https://trynextplay.app"


@router.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots_txt() -> str:
    """Tell crawlers what to index. Blocks API/admin/org/auth-gated paths."""
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /admin/\n"
        "Disallow: /org/\n"
        "Disallow: /auth/\n"
        "Disallow: /checkout/\n"
        "Disallow: /upgrade\n"
        "Disallow: /profile\n"
        "Disallow: /settings\n"
        "Disallow: /chat\n"
        "Disallow: /history\n"
        "Disallow: /notebook\n"
        "Disallow: /team-setup\n"
        "Disallow: /data-upload\n"
        "Disallow: /scouting\n"
        "Disallow: /plays\n"
        "Disallow: /club-admin\n"
        "Disallow: /court-preview\n"
        "Disallow: /player/\n"
        "Disallow: /clip/\n"
        "Disallow: /share/\n"
        "\n"
        f"Sitemap: {_SITE_URL}/sitemap.xml\n"
    )


@router.get("/sitemap.xml", response_class=Response, include_in_schema=False)
async def sitemap_xml() -> Response:
    """List the public, indexable pages. Update this list as new SEO landing
    pages are added (scouting-report, practice-planner, etc.)."""
    pages: list[tuple[str, str, str]] = [
        # (path, changefreq, priority)
        ("/", "weekly", "1.0"),
        ("/main", "weekly", "1.0"),
        ("/play-creator", "weekly", "0.9"),
        ("/login", "monthly", "0.4"),
        ("/register", "monthly", "0.6"),
        ("/contact-sales", "monthly", "0.5"),
        ("/privacy", "yearly", "0.2"),
        ("/terms", "yearly", "0.2"),
    ]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path, freq, pri in pages:
        lines.append(
            f"  <url><loc>{_SITE_URL}{path}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{pri}</priority></url>"
        )
    lines.append("</urlset>")
    return Response("\n".join(lines), media_type="application/xml")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
) -> Any:
    return templates.TemplateResponse(
        "privacy.html", page_context(request, user=user),
    )


@router.get("/terms", response_class=HTMLResponse)
async def terms_page(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
) -> Any:
    return templates.TemplateResponse(
        "terms.html", page_context(request, user=user),
    )


@router.get("/contact-sales", response_class=HTMLResponse)
async def contact_sales_page(
    request: Request,
    plan: str = "enterprise",
    user: User | None = Depends(get_current_user_optional),
) -> Any:
    if plan == "academy10":
        plan = "academy"
    if plan not in ("academy", "enterprise"):
        plan = "enterprise"
    labels = {"academy": "Academy", "enterprise": "Enterprise"}
    return templates.TemplateResponse(
        "contact_sales.html",
        page_context(request, user=user, extra={
            "plan": plan, "plan_label": labels[plan],
        }),
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
) -> Any:
    """Render the login form. Logged-in users go straight to /.
    Includes OAuth provider flags so the template can render the
    social-login buttons (Google / Facebook / Apple)."""
    if user is not None:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        page_context(request, user=None, extra=_oauth_flags()),
    )


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
) -> Any:
    if user is not None:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        "register.html",
        page_context(request, user=None, extra=_oauth_flags()),
    )


# ---------------------------------------------------------------------------
# Authed pages — each builds the same context v1 built
# ---------------------------------------------------------------------------


async def _team_data(db: AsyncSession, user: User) -> dict[str, Any]:
    """Fetch the navbar + dashboard inputs every authed page needs.
    Mirrors the boilerplate at the top of every v1 page route."""
    from src.models.memory import SessionSummary
    from src.models.players import Player
    from src.models.teams import TeamProfile
    from src.models.uploads import Upload

    tid = user.active_team_id

    profile = None
    players: list = []
    uploads: list = []
    sessions: list = []
    teams_list: list = []

    if tid is not None:
        profile = (await db.execute(
            select(TeamProfile).where(TeamProfile.id == tid)
        )).scalar_one_or_none()
        players = list((await db.execute(
            select(Player)
            .where(Player.user_id == user.id, Player.team_id == tid, Player.active.is_(True))
            .order_by(Player.number.is_(None), Player.number)
        )).scalars().all())
        uploads = list((await db.execute(
            select(Upload)
            .where(Upload.user_id == user.id, Upload.team_id == tid)
            .order_by(Upload.uploaded_at.desc().nulls_last())
        )).scalars().all())
        try:
            sessions = list((await db.execute(
                select(SessionSummary)
                .where(SessionSummary.user_id == user.id, SessionSummary.team_id == tid)
                .order_by(SessionSummary.created_at.desc().nulls_last())
                .limit(50)
            )).scalars().all())
        except Exception:
            sessions = []

    teams_list = list((await db.execute(
        select(TeamProfile)
        .where(TeamProfile.user_id == user.id)
        .order_by(TeamProfile.updated_at.desc().nulls_last())
    )).scalars().all())

    return {
        "profile": profile,
        "players": players,
        "uploads": uploads,
        "sessions": sessions,
        "teams": teams_list,
        "active_team_id": tid,
    }


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Authed dashboard. Anonymous visitors are redirected to /main
    (the public landing) — mirrors v1's `@login_required` redirect
    behavior, which `get_current_user` doesn't reproduce on its own
    (it raises a 401 JSON response, which is wrong for an HTML route)."""
    if user is None:
        return RedirectResponse(url="/main", status_code=303)
    extra = await _team_data(db, user)
    return templates.TemplateResponse(
        "home.html", page_context(request, user=user, extra=extra),
    )


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    session: str | None = None,
    agent: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    data = await _team_data(db, user)
    if not data["profile"]:
        return templates.TemplateResponse(
            "home.html", page_context(request, user=user, extra=data),
        )

    session_id = session or (
        datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    )

    from src.models.conversations import Conversation
    msgs = list((await db.execute(
        select(Conversation)
        .where(
            Conversation.session_id == session_id,
            Conversation.user_id == user.id,
        )
        .order_by(Conversation.id)
    )).scalars().all())

    extra = {
        **data,
        "session_id": session_id,
        "auto_agent": agent,
        "messages": msgs,
    }
    return templates.TemplateResponse(
        "chat.html", page_context(request, user=user, extra=extra),
    )


@router.get("/team-setup", response_class=HTMLResponse)
async def team_setup(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    data = await _team_data(db, user)
    if not data["profile"]:
        data["profile"] = {}

    # player_metrics is a per-id dict of metrics. Template pipes this
    # through `| tojson`, so values MUST be plain dicts — ORM objects
    # aren't JSON-serializable. Surface the metrics_json blob directly,
    # matching v1's dict-row shape.
    from src.models.players import PlayerMetric
    metrics_map: dict[int, dict[str, Any]] = {}
    if data["players"]:
        rows = list((await db.execute(
            select(PlayerMetric).where(
                PlayerMetric.player_id.in_([p.id for p in data["players"]])
            )
        )).scalars().all())
        for m in rows:
            metrics_map[m.player_id] = (m.metrics_json or {})

    extra = {**data, "player_metrics": metrics_map}
    return templates.TemplateResponse(
        "team_setup.html", page_context(request, user=user, extra=extra),
    )


@router.get("/player/{player_id}", response_class=HTMLResponse)
async def player_profile(
    request: Request,
    player_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    from src.models.players import Player, PlayerMetric

    player = (await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == user.id)
    )).scalar_one_or_none()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    metric_row = (await db.execute(
        select(PlayerMetric).where(PlayerMetric.player_id == player_id)
    )).scalar_one_or_none()
    # Template does `{{ metrics | tojson }}` to seed the slider JS — pass the
    # raw dict, not the ORM object (which `tojson` can't serialize).
    metrics = (metric_row.metrics_json or {}) if metric_row else {}

    photo_url = None
    if player.photo_url and player.photo_url.startswith("players/"):
        try:
            from src.services.s3 import get_video_url

            photo_url = await get_video_url(player.photo_url)
        except Exception:
            photo_url = None

    data = await _team_data(db, user)
    extra = {
        **data,
        "player": player,
        "metrics": metrics,
        "photo_url": photo_url,
    }
    return templates.TemplateResponse(
        "player.html", page_context(request, user=user, extra=extra),
    )


@router.get("/data-upload", response_class=HTMLResponse)
async def data_upload(
    request: Request,
    user: User = Depends(get_current_user),
) -> Any:
    return templates.TemplateResponse(
        "data_upload.html", page_context(request, user=user),
    )


@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    try:
        data = await _team_data(db, user)
    except Exception:
        data = {"profile": None, "players": [], "uploads": [], "sessions": [],
                "teams": [], "active_team_id": None}
    return templates.TemplateResponse(
        "history.html", page_context(request, user=user, extra=data),
    )


@router.get("/profile", response_class=HTMLResponse)
async def coach_profile_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    raw_avatar = (user.avatar_url or "").strip()
    avatar_display_url = None
    if raw_avatar.startswith("avatars/"):
        try:
            from src.services.s3 import get_video_url

            avatar_display_url = await get_video_url(raw_avatar)
        except Exception:
            avatar_display_url = None
    elif raw_avatar.startswith("http"):
        avatar_display_url = raw_avatar

    from sqlalchemy import func as sql_func

    from src.models.coach import Feedback
    from src.models.conversations import Conversation
    from src.models.memory import SessionSummary

    total_messages = (await db.execute(
        select(sql_func.count()).select_from(Conversation).where(Conversation.user_id == user.id)
    )).scalar_one()
    try:
        total_sessions = (await db.execute(
            select(sql_func.count()).select_from(SessionSummary).where(SessionSummary.user_id == user.id)
        )).scalar_one()
    except Exception:
        total_sessions = 0
    total_feedback = (await db.execute(
        select(sql_func.count()).select_from(Feedback).where(Feedback.user_id == user.id)
    )).scalar_one()

    data = await _team_data(db, user)
    extra = {
        **data,
        "user_profile": user,
        "avatar_display_url": avatar_display_url,
        "total_messages": total_messages,
        "total_sessions": total_sessions,
        "total_feedback": total_feedback,
    }
    return templates.TemplateResponse(
        "coach_profile.html", page_context(request, user=user, extra=extra),
    )


@router.get("/settings", response_class=HTMLResponse)
async def coach_settings_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    from src.models.coach import CoachPreference, Feedback

    prefs = (await db.execute(
        select(CoachPreference).where(CoachPreference.user_id == user.id)
    )).scalar_one_or_none()
    feedback = list((await db.execute(
        select(Feedback)
        .where(Feedback.user_id == user.id)
        .order_by(Feedback.created_at.desc().nulls_last())
        .limit(20)
    )).scalars().all())

    data = await _team_data(db, user)
    extra = {**data, "prefs": prefs or {}, "feedback": feedback}
    return templates.TemplateResponse(
        "coach_settings.html", page_context(request, user=user, extra=extra),
    )


@router.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(
    request: Request,
    user: User = Depends(get_current_user),
) -> Any:
    return templates.TemplateResponse(
        "upgrade.html", page_context(request, user=user),
    )


@router.get("/checkout/{plan}", response_class=HTMLResponse)
async def checkout_page(
    request: Request,
    plan: str,
    billing: str = "monthly",
    user: User = Depends(get_current_user),
) -> Any:
    if plan not in ("basic", "pro"):
        return RedirectResponse(url="/upgrade", status_code=302)
    if billing not in ("monthly", "yearly"):
        billing = "monthly"
    prices = {
        "basic": {"monthly": 19, "yearly": 190},
        "pro": {"monthly": 29, "yearly": 290},
    }
    return templates.TemplateResponse(
        "checkout.html",
        page_context(request, user=user, extra={
            "plan": plan, "billing": billing,
            "price": prices[plan][billing],
        }),
    )


@router.get("/court-preview", response_class=HTMLResponse)
async def court_preview(
    request: Request,
    user: User = Depends(get_current_user),
) -> Any:
    return templates.TemplateResponse(
        "court_preview.html", page_context(request, user=user),
    )


@router.get("/club-admin", response_class=HTMLResponse)
async def club_admin(
    request: Request,
    user: User = Depends(get_current_user),
) -> Any:
    if not getattr(user, "is_club_admin", False):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        "club_admin.html", page_context(request, user=user),
    )


# ---------------------------------------------------------------------------
# Pro-feature pages — Scouting Room + Plays Studio
# ---------------------------------------------------------------------------


@router.get("/scouting", response_class=HTMLResponse)
async def scouting_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Scouting Room shell. Pro-gated in v1; for the local-dev port we
    allow any authed user — gating moves to the API endpoints. The page
    JS hits `/api/scouting/*` for video CRUD + uploads."""
    data = await _team_data(db, user)
    return templates.TemplateResponse(
        "scouting.html", page_context(request, user=user, extra=data),
    )


@router.get("/plays", response_class=HTMLResponse)
async def plays_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Plays Studio shell. JS hits `/api/plays/*` for play CRUD and
    `/api/plays/{id}/share` for public link generation."""
    data = await _team_data(db, user)
    return templates.TemplateResponse(
        "plays.html", page_context(request, user=user, extra=data),
    )


@router.get("/notebook", response_class=HTMLResponse)
async def notebook_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Coach Notebook shell. JS hits `/api/notebook/*` for entries
    (practice plans, game summaries, attendance)."""
    data = await _team_data(db, user)
    # Template does `{{ players | tojson }}` for client-side JS — Jinja's
    # tojson can't serialize SQLAlchemy ORM objects, so pre-flatten the
    # roster to the dict shape the JS expects.
    data["players"] = [
        {
            "id": p.id,
            "name": p.name or "",
            "number": p.number,
            "position": p.position or "",
        }
        for p in (data.get("players") or [])
    ]
    return templates.TemplateResponse(
        "notebook.html", page_context(request, user=user, extra=data),
    )


@router.get("/play/{token}", response_class=HTMLResponse)
async def public_play_page(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Public viewer for a shared play diagram. No auth — the share
    token is the access control. 404 page renders when the token
    doesn't match a saved share. Mirrors v1 plays/routes.py:95."""
    from src.models.plays import PlayShare

    share = (await db.execute(
        select(PlayShare).where(PlayShare.token == token)
    )).scalar_one_or_none()

    play_data = (share.play_json if share is not None else None)
    status_code = 200 if share is not None else 404

    return templates.TemplateResponse(
        "play_share.html",
        page_context(request, user=None, extra={"play_data": play_data}),
        status_code=status_code,
    )


@router.get("/share/{token}", response_class=HTMLResponse)
async def public_clip_share_page(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Public viewer for shared clips (scouting room). No auth — the
    share token is the access control. Renders `clip_share.html` with
    server-injected `video` / `share` / `clips` / `annotations` /
    `timeline` context so the template can `{{ ... | tojson }}` them
    without a follow-up API call. Mirrors v1 backend/scouting/routes.py
    :577 (public_share_page)."""
    from src.api.scouting import (
        _hydrate_playback_url,
        _serialize_annotation,
        _serialize_clip,
        _serialize_video,
    )
    from src.models.scouting import (
        ClipShare,
        ScoutingVideo,
        VideoAnnotation,
        VideoClip,
    )

    share = (await db.execute(
        select(ClipShare).where(ClipShare.share_token == token)
    )).scalar_one_or_none()
    if not share:
        return templates.TemplateResponse(
            "clip_share.html",
            page_context(request, user=None, extra={"error": True}),
            status_code=404,
        )

    video = (await db.execute(
        select(ScoutingVideo).where(ScoutingVideo.id == share.video_id)
    )).scalar_one_or_none()
    if not video:
        return templates.TemplateResponse(
            "clip_share.html",
            page_context(request, user=None, extra={"error": True}),
            status_code=404,
        )

    clip_ids = [int(x) for x in (share.clip_ids or "").split(",") if x.strip()]
    clips = list((await db.execute(
        select(VideoClip).where(VideoClip.id.in_(clip_ids))
        .order_by(VideoClip.start_time)
    )).scalars().all()) if clip_ids else []
    annotations = list((await db.execute(
        select(VideoAnnotation).where(VideoAnnotation.video_id == share.video_id)
        .order_by(VideoAnnotation.timestamp)
    )).scalars().all())

    video_dict = await _hydrate_playback_url(_serialize_video(video), video)

    return templates.TemplateResponse(
        "clip_share.html",
        page_context(request, user=None, extra={
            "error": False,
            "video": video_dict,
            "share": {
                "token": share.share_token,
                "video_id": share.video_id,
                "created_at": share.created_at,
            },
            "clips": [_serialize_clip(c) for c in clips],
            "annotations": [_serialize_annotation(a) for a in annotations],
            "timeline": share.timeline_json,
        }),
    )


__all__ = ["router"]
