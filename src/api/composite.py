"""Composite GET endpoints — aggregated data for SPA pages.

Async port of `backend/api/composite.py`. Each endpoint batches what
v1 fetches via the Jinja2 page render — once we move the SPA off
server-side templates these become the canonical data sources.

We use `asyncio.gather` for the parallel-fetchable parts. v1 was
sequential; this is a free perf win because the trips are independent.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.database import get_db
from src.models.coach import CoachPreference, Feedback
from src.models.conversations import Conversation
from src.models.players import Player, PlayerMetric
from src.models.teams import TeamProfile
from src.models.uploads import Upload
from src.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["composite"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _serialize_team(t: TeamProfile) -> dict:
    return {
        "id": t.id, "team_name": t.team_name,
        "league": t.league or "", "division": t.division or "",
    }


def _serialize_player(p: Player) -> dict:
    return {
        "id": p.id, "name": p.name, "number": p.number,
        "position": p.position or "", "height": p.height or "",
        "weight": p.weight or "", "age": p.age,
        "active": bool(p.active), "photo_url": p.photo_url or "",
    }


def _serialize_upload(u: Upload) -> dict:
    return {
        "id": u.id, "filename": u.filename, "file_type": u.file_type,
        "uploaded_at": u.uploaded_at.isoformat() if u.uploaded_at else None,
    }


def _serialize_full_team_profile(t: TeamProfile) -> dict:
    return {
        "id": t.id, "team_name": t.team_name,
        "league": t.league or "", "division": t.division or "",
        "play_style": t.play_style or "",
        "strengths": t.strengths or "", "weaknesses": t.weaknesses or "",
        "notes": t.notes or "",
        "extra_storage_gb": t.extra_storage_gb or 0,
    }


async def _fetch_active_team_profile(db: AsyncSession, team_id: int | None):
    if team_id is None:
        return None
    return (await db.execute(
        select(TeamProfile).where(TeamProfile.id == team_id)
    )).scalar_one_or_none()


async def _fetch_active_players(db: AsyncSession, team_id: int | None) -> list[Player]:
    if team_id is None:
        return []
    return list((await db.execute(
        select(Player)
        .where(Player.team_id == team_id, Player.active.is_(True))
        .order_by(Player.number.is_(None), Player.number, Player.name)
    )).scalars().all())


async def _fetch_uploads(db: AsyncSession, team_id: int | None) -> list[Upload]:
    if team_id is None:
        return []
    return list((await db.execute(
        select(Upload).where(Upload.team_id == team_id)
        .order_by(Upload.uploaded_at.desc().nulls_last())
    )).scalars().all())


async def _fetch_user_teams(db: AsyncSession, user_id: int) -> list[TeamProfile]:
    return list((await db.execute(
        select(TeamProfile).where(TeamProfile.user_id == user_id)
        .order_by(TeamProfile.id)
    )).scalars().all())


def _trial_days_left(trial_ends_at: str | None) -> int:
    if not trial_ends_at:
        return 0
    try:
        ends = datetime.fromisoformat(trial_ends_at)
    except (ValueError, TypeError):
        return 0
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=UTC)
    delta = ends - datetime.now(UTC)
    return max(0, delta.days)


# ---------------------------------------------------------------------------
# /api/me — current user + sidebar primer
# ---------------------------------------------------------------------------

@router.get("/me")
async def api_me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    teams, profile, players, uploads = await asyncio.gather(
        _fetch_user_teams(db, user.id),
        _fetch_active_team_profile(db, user.active_team_id),
        _fetch_active_players(db, user.active_team_id),
        _fetch_uploads(db, user.active_team_id),
    )

    plan = user.subscription_plan or "trial"
    days_left = _trial_days_left(user.trial_ends_at) if plan == "trial" else 0

    return {
        "user": {
            "id": user.id, "email": user.email,
            "display_name": user.display_name,
            "role": user.role, "avatar_url": user.avatar_url or "",
        },
        "teams": [_serialize_team(t) for t in teams],
        "active_team_id": user.active_team_id,
        "profile": _serialize_full_team_profile(profile) if profile else None,
        "player_count": len(players),
        "upload_count": len(uploads),
        "subscription_plan": plan,
        "trial_days_left": days_left,
    }


# ---------------------------------------------------------------------------
# /api/dashboard — home page primer
# ---------------------------------------------------------------------------

@router.get("/dashboard")
async def api_dashboard(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    teams, profile, players, uploads, sessions = await asyncio.gather(
        _fetch_user_teams(db, user.id),
        _fetch_active_team_profile(db, user.active_team_id),
        _fetch_active_players(db, user.active_team_id),
        _fetch_uploads(db, user.active_team_id),
        _fetch_session_summaries(db, user.active_team_id),
    )
    return {
        "profile": _serialize_full_team_profile(profile) if profile else None,
        "players": [_serialize_player(p) for p in players],
        "uploads": [_serialize_upload(u) for u in uploads],
        "teams": [_serialize_team(t) for t in teams],
        "active_team_id": user.active_team_id,
        "sessions": sessions,
    }


async def _fetch_session_summaries(db: AsyncSession, team_id: int | None) -> list[dict]:
    """Distinct session_ids in conversations, newest-first. Mirrors v1
    `get_all_sessions` shape (id + first_message + count)."""
    if team_id is None:
        return []
    rows = (await db.execute(
        select(
            Conversation.session_id,
            func.max(Conversation.created_at).label("last_at"),
            func.count(Conversation.id).label("msg_count"),
        )
        .where(Conversation.team_id == team_id)
        .group_by(Conversation.session_id)
        .order_by(func.max(Conversation.created_at).desc())
        .limit(50)
    )).all()
    return [
        {
            "session_id": r.session_id,
            "last_at": r.last_at.isoformat() if r.last_at else None,
            "msg_count": int(r.msg_count or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# /api/team-setup/data — team setup page primer
# ---------------------------------------------------------------------------

@router.get("/team-setup/data")
async def api_team_setup_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    teams, profile, players = await asyncio.gather(
        _fetch_user_teams(db, user.id),
        _fetch_active_team_profile(db, user.active_team_id),
        _fetch_active_players(db, user.active_team_id),
    )
    metrics = await _fetch_player_metrics(db, user.active_team_id)
    return {
        "profile": _serialize_full_team_profile(profile) if profile else {},
        "players": [_serialize_player(p) for p in players],
        "player_metrics": metrics,
        "teams": [_serialize_team(t) for t in teams],
        "active_team_id": user.active_team_id,
    }


async def _fetch_player_metrics(db: AsyncSession, team_id: int | None) -> dict:
    if team_id is None:
        return {}
    rows = (await db.execute(
        select(PlayerMetric.player_id, PlayerMetric.metrics_json)
        .where(PlayerMetric.team_id == team_id)
    )).all()
    return {str(r.player_id): r.metrics_json or {} for r in rows}


# ---------------------------------------------------------------------------
# /api/coach/profile-data + /api/coach/settings-data
# ---------------------------------------------------------------------------

@router.get("/coach/profile-data")
async def api_coach_profile_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    teams, total_messages, total_sessions, total_feedback = await asyncio.gather(
        _fetch_user_teams(db, user.id),
        _scalar(db, select(func.count()).select_from(Conversation).where(
            Conversation.user_id == user.id,
        )),
        _scalar(db, select(func.count(func.distinct(Conversation.session_id))).where(
            Conversation.user_id == user.id,
        )),
        _scalar(db, select(func.count()).select_from(Feedback).where(
            Feedback.user_id == user.id,
        )),
    )
    return {
        "user_profile": {
            "email": user.email,
            "display_name": user.display_name or "Coach",
            "role": user.role or "coach",
            "created_at": user.created_at,
            "avatar_url": user.avatar_url or "",
        },
        "teams": [_serialize_team(t) for t in teams],
        "active_team_id": user.active_team_id,
        "total_messages": int(total_messages or 0),
        "total_sessions": int(total_sessions or 0),
        "total_feedback": int(total_feedback or 0),
    }


async def _scalar(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar() or 0)


@router.get("/coach/settings-data")
async def api_coach_settings_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    prefs = (await db.execute(
        select(CoachPreference).where(CoachPreference.user_id == user.id)
    )).scalar_one_or_none()
    feedback_rows = list((await db.execute(
        select(Feedback)
        .where(Feedback.user_id == user.id)
        .order_by(Feedback.created_at.desc().nulls_last())
        .limit(20)
    )).scalars().all())
    teams = await _fetch_user_teams(db, user.id)

    return {
        "prefs": {
            "preferred_language": prefs.preferred_language if prefs else "en",
            "detail_level": prefs.detail_level if prefs else "medium",
            "focus_areas": prefs.focus_areas if prefs else "",
            "avoid_topics": prefs.avoid_topics if prefs else "",
            "practice_duration": prefs.practice_duration if prefs else 90,
            "coaching_style": prefs.coaching_style if prefs else "",
            "custom_notes": prefs.custom_notes if prefs else "",
        } if prefs else {},
        "feedback": [
            {
                "id": f.id, "rating": f.rating, "comment": f.comment or "",
                "agent_key": f.agent_key,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in feedback_rows
        ],
        "teams": [_serialize_team(t) for t in teams],
        "active_team_id": user.active_team_id,
    }


# ---------------------------------------------------------------------------
# /api/history/data — chat history list primer
# ---------------------------------------------------------------------------

@router.get("/history/data")
async def api_history_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    teams, profile, players, uploads, sessions = await asyncio.gather(
        _fetch_user_teams(db, user.id),
        _fetch_active_team_profile(db, user.active_team_id),
        _fetch_active_players(db, user.active_team_id),
        _fetch_uploads(db, user.active_team_id),
        _fetch_session_summaries(db, user.active_team_id),
    )
    return {
        "profile": _serialize_full_team_profile(profile) if profile else None,
        "players": [_serialize_player(p) for p in players],
        "uploads": [_serialize_upload(u) for u in uploads],
        "teams": [_serialize_team(t) for t in teams],
        "active_team_id": user.active_team_id,
        "sessions": sessions,
    }


# ---------------------------------------------------------------------------
# /api/player/{id}/data — player profile page primer
# ---------------------------------------------------------------------------

@router.get("/player/{player_id}/data")
async def api_player_data(
    player_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    player = (await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == user.id)
    )).scalar_one_or_none()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    metrics_row, profile, all_players, uploads, teams = await asyncio.gather(
        _fetch_player_metrics_row(db, player_id),
        _fetch_active_team_profile(db, user.active_team_id),
        _fetch_active_players(db, user.active_team_id),
        _fetch_uploads(db, user.active_team_id),
        _fetch_user_teams(db, user.id),
    )

    return {
        "player": _serialize_player(player),
        "metrics": metrics_row,
        "profile": _serialize_full_team_profile(profile) if profile else None,
        "players": [_serialize_player(p) for p in all_players],
        "uploads": [_serialize_upload(u) for u in uploads],
        "teams": [_serialize_team(t) for t in teams],
        "active_team_id": user.active_team_id,
    }


async def _fetch_player_metrics_row(db: AsyncSession, player_id: int) -> dict | None:
    row = (await db.execute(
        select(PlayerMetric.metrics_json).where(PlayerMetric.player_id == player_id)
    )).scalar_one_or_none()
    return row or None


# ---------------------------------------------------------------------------
# /api/chat/init — chat page bootstrap
# ---------------------------------------------------------------------------

@router.get("/chat/init")
async def api_chat_init(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session: str | None = None,
    agent: str | None = None,
) -> dict:
    if user.active_team_id is None:
        return {"redirect": True, "profile": None}

    session_id = session or (
        datetime.utcnow().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    )

    profile, players, uploads, teams, messages = await asyncio.gather(
        _fetch_active_team_profile(db, user.active_team_id),
        _fetch_active_players(db, user.active_team_id),
        _fetch_uploads(db, user.active_team_id),
        _fetch_user_teams(db, user.id),
        _fetch_session_messages(db, session_id, user.active_team_id),
    )
    if not profile:
        return {"redirect": True, "profile": None}

    return {
        "profile": _serialize_full_team_profile(profile),
        "session_id": session_id,
        "messages": messages,
        "auto_agent": agent,
        "players": [_serialize_player(p) for p in players],
        "uploads": [_serialize_upload(u) for u in uploads],
        "teams": [_serialize_team(t) for t in teams],
        "active_team_id": user.active_team_id,
    }


async def _fetch_session_messages(
    db: AsyncSession, session_id: str, team_id: int
) -> list[dict]:
    rows = list((await db.execute(
        select(Conversation)
        .where(Conversation.session_id == session_id, Conversation.team_id == team_id)
        .order_by(Conversation.created_at)
    )).scalars().all())
    return [
        {
            "id": c.id, "role": c.role, "content": c.content,
            "agent_used": c.agent_used,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in rows
    ]


# ---------------------------------------------------------------------------
# /api/uploads/list — uploads page primer
# ---------------------------------------------------------------------------

@router.get("/uploads/list")
async def api_uploads_list(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    teams, uploads = await asyncio.gather(
        _fetch_user_teams(db, user.id),
        _fetch_uploads(db, user.active_team_id),
    )
    return {
        "uploads": [_serialize_upload(u) for u in uploads],
        "teams": [_serialize_team(t) for t in teams],
        "active_team_id": user.active_team_id,
    }
