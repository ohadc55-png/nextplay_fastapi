"""Coach profile / settings / feedback / avatar.

Async port of `backend/api/coach.py`. Avatar upload (S3) is deferred to
Phase 6; the DELETE endpoint here only clears the URL column. Feedback
captures the thumbs up/down with truncated message + response (mirrors
v1's 500-char cap) so the admin feedback dashboard has context.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.database import get_db
from src.models.coach import CoachPreference, Feedback
from src.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["coach"])


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

class _ProfileUpdateBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)


@router.post("/coach/profile")
async def update_profile(
    body: _ProfileUpdateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update the coach's display name. Trims + truncates server-side."""
    user.display_name = body.display_name.strip()[:255]
    await db.flush()
    return {"ok": True, "display_name": user.display_name}


# ---------------------------------------------------------------------------
# Settings (coach preferences)
# ---------------------------------------------------------------------------

class _SettingsUpdateBody(BaseModel):
    preferred_language: str | None = None
    detail_level: str | None = None
    focus_areas: str | None = None
    avoid_topics: str | None = None
    practice_duration: int | None = None
    coaching_style: str | None = None
    custom_notes: str | None = None


@router.post("/coach/settings")
async def save_settings(
    body: _SettingsUpdateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Save coach preferences. Upsert on UNIQUE(user_id) — first write
    creates the row, subsequent writes update."""
    prefs = (await db.execute(
        select(CoachPreference).where(CoachPreference.user_id == user.id)
    )).scalar_one_or_none()

    data = body.model_dump(exclude_unset=True)
    if not prefs:
        prefs = CoachPreference(user_id=user.id, **data)
        db.add(prefs)
    else:
        for key, value in data.items():
            setattr(prefs, key, value)
        prefs.updated_at = datetime.utcnow()
    await db.flush()

    return {
        "ok": True,
        "preferred_language": prefs.preferred_language,
        "detail_level": prefs.detail_level,
        "focus_areas": prefs.focus_areas,
        "avoid_topics": prefs.avoid_topics,
        "practice_duration": prefs.practice_duration,
        "coaching_style": prefs.coaching_style,
        "custom_notes": prefs.custom_notes,
    }


# ---------------------------------------------------------------------------
# Feedback (thumbs up/down on an agent response)
# ---------------------------------------------------------------------------

class _FeedbackBody(BaseModel):
    rating: int  # +1 or -1
    comment: str | None = ""
    agent: str | None = "auto"
    message: str | None = ""
    response: str | None = ""


@router.post("/feedback")
async def save_feedback(
    body: _FeedbackBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.rating not in (-1, 1):
        raise HTTPException(status_code=400, detail="rating must be +1 or -1")

    fb = Feedback(
        user_id=user.id,
        team_id=user.active_team_id,
        agent_key=(body.agent or "auto").strip(),
        message_content=(body.message or "")[:500],
        response_content=(body.response or "")[:500],
        rating=body.rating,
        comment=(body.comment or "").strip(),
    )
    db.add(fb)
    await db.flush()
    return {"ok": True, "id": fb.id}


# ---------------------------------------------------------------------------
# Avatar — DELETE only (upload deferred to Phase 6 + S3)
# ---------------------------------------------------------------------------

@router.delete("/coach/avatar")
async def delete_avatar(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Clear users.avatar_url. Phase 6 will also delete the S3 object if
    the URL is one of OUR uploads (avatars/...). For now the DB clear is
    enough; orphaned S3 objects can be reaped by a sweeper."""
    await db.execute(
        update(User).where(User.id == user.id).values(avatar_url=None)
    )
    await db.flush()
    return {"ok": True}
