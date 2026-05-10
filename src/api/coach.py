"""Coach profile / settings / feedback / avatar.

Async port of `backend/api/coach.py`. Phase 6 wires the avatar upload:
PIL crop + resize + WebP encode (CPU-bound; off-thread) → S3 put under
`avatars/<uuid>.webp` → `users.avatar_url` updated to the S3 key. The
DELETE endpoint clears the URL and removes the S3 object for OUR
uploads (an avatar URL that doesn't start with `avatars/` came from
the OAuth provider and is left alone).
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
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
# Avatar — POST upload + DELETE clear (Phase 6 batch 4)
# ---------------------------------------------------------------------------

# v1 caps. 256px is 2x retina at 128px display sizes; WebP @ q=86 keeps
# avatars under ~30 KB on disk.
_AVATAR_MAX_BYTES = 8 * 1024 * 1024
_AVATAR_OUT_SIZE = 256
_AVATAR_ALLOWED_EXT = ("jpg", "jpeg", "png", "webp")


def _process_avatar_sync(raw: bytes) -> bytes:
    """Open + center-crop to square + resize + WebP-encode. Sync because
    Pillow is sync; called via `asyncio.to_thread`. Mirrors v1's
    `_process_avatar` (backend/api/coach.py:54-77) byte-for-byte."""
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")  # drop alpha — WebP RGB is smaller + universal

    # Center-crop to square based on the shorter side, THEN resize. Avoids
    # the squashed-face look you get from naively resizing a portrait.
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((_AVATAR_OUT_SIZE, _AVATAR_OUT_SIZE), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=86, method=6)
    return buf.getvalue()


@router.post("/coach/avatar")
async def upload_avatar(
    photo: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Upload a coach avatar. PIL processes (crop → resize → WebP);
    result lands in S3 under `avatars/<uuid>.webp`. The previous
    avatar (if it was one of OUR uploads — `avatars/...`) is deleted.
    OAuth-provided avatar URLs (https://...) are NEVER touched.

    Returns the new avatar key — the SPA composes the playback URL via
    `/api/coach/avatar-url` (or the upload-config CDN domain)."""
    from src.services import s3 as s3_module

    if not s3_module.is_configured():
        raise HTTPException(status_code=503, detail="S3 is not configured on this server")

    if not photo.filename:
        raise HTTPException(status_code=400, detail="Empty filename")
    ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else ""
    if ext not in _AVATAR_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="Use JPG, PNG, or WebP")

    # Read with a hard cap — anything over the cap is rejected.
    raw = await photo.read(_AVATAR_MAX_BYTES + 1)
    if len(raw) > _AVATAR_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Image too large (max 8 MB)")

    try:
        # PIL is sync + CPU-heavy. Off-thread so the event loop stays free
        # while a 4MB photo is being decoded + re-encoded.
        processed = await asyncio.to_thread(_process_avatar_sync, raw)
    except Exception as e:
        logger.warning("[avatar] PIL failed for uid=%s: %s", user.id, e)
        raise HTTPException(
            status_code=400,
            detail="Could not read image — try a different file",
        )

    new_key = f"avatars/{uuid.uuid4().hex}.webp"
    try:
        await s3_module.put_bytes(
            key=new_key, data=processed, content_type="image/webp",
        )
    except Exception as e:
        logger.exception("[avatar] S3 put failed for uid=%s: %s", user.id, e)
        raise HTTPException(status_code=500, detail="Upload failed — try again")

    # Clean up the previous avatar if it was one of OURS. OAuth URLs
    # (https://lh3.googleusercontent.com/...) start with https — never
    # try to S3-delete those.
    old = (user.avatar_url or "").strip()
    if old.startswith("avatars/"):
        await s3_module.delete_object(old)  # best-effort

    await db.execute(
        update(User).where(User.id == user.id).values(avatar_url=new_key)
    )
    await db.flush()
    return {"ok": True, "avatar_key": new_key}


@router.delete("/coach/avatar")
async def delete_avatar(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Clear users.avatar_url. If the current avatar is one of OUR
    uploads (key starts with `avatars/`), also delete the S3 object.
    OAuth-provided avatar URLs are simply unlinked — we don't own
    those photos."""
    old = (user.avatar_url or "").strip()
    if old.startswith("avatars/"):
        from src.services import s3 as s3_module

        await s3_module.delete_object(old)  # best-effort

    await db.execute(
        update(User).where(User.id == user.id).values(avatar_url=None)
    )
    await db.flush()
    return {"ok": True}
