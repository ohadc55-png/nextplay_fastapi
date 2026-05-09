"""Play Creator router — async port of `backend/plays/routes.py` + `service.py`.

Endpoints:
  GET    /api/plays                       list user's plays (newest first)
  POST   /api/plays                       create
  GET    /api/plays/{id}                  fetch
  PUT    /api/plays/{id}                  update
  DELETE /api/plays/{id}                  delete
  POST   /api/plays/share                 snapshot a play, return token + URL
  GET    /play/{token}                    public read-only share (no auth)

Multi-tenancy: list/get/update/delete enforce `user_id` ownership.
team_id is captured at create time from the user's active team but
isn't required for the listing — v1 lists by user only.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.database import get_db
from src.models.plays import Play, PlayShare
from src.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plays"])


def _serialize_play(p: Play) -> dict:
    return {
        "id": p.id,
        "user_id": p.user_id,
        "team_id": p.team_id,
        "name": p.name,
        "description": p.description or "",
        "offense_template": p.offense_template or "empty",
        "defense_template": p.defense_template or "none",
        "players": p.players_json or [],
        "actions": p.actions_json or [],
        "ball_holder_id": p.ball_holder_id,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class _PlayCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = ""
    offense_template: str | None = "empty"
    defense_template: str | None = "none"
    players: list[Any] = Field(default_factory=list)
    actions: list[Any] = Field(default_factory=list)
    ball_holder_id: str | None = None


class _PlayUpdateBody(BaseModel):
    name: str | None = None
    description: str | None = None
    offense_template: str | None = None
    defense_template: str | None = None
    players: list[Any] | None = None
    actions: list[Any] | None = None
    ball_holder_id: str | None = None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("/api/plays")
async def list_plays(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """User's plays, newest-updated first."""
    rows = list((await db.execute(
        select(Play)
        .where(Play.user_id == user.id)
        .order_by(Play.updated_at.desc())
    )).scalars().all())
    return {"success": True, "data": [_serialize_play(r) for r in rows]}


@router.post("/api/plays", status_code=201)
async def create_play(
    body: _PlayCreateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    now = datetime.utcnow().isoformat()
    play = Play(
        user_id=user.id,
        team_id=user.active_team_id,
        name=body.name.strip() or "Untitled",
        description=(body.description or "").strip(),
        offense_template=body.offense_template or "empty",
        defense_template=body.defense_template or "none",
        players_json=body.players or [],
        actions_json=body.actions or [],
        ball_holder_id=body.ball_holder_id,
        created_at=now,
        updated_at=now,
    )
    db.add(play)
    await db.flush()
    return {"success": True, "data": _serialize_play(play)}


@router.get("/api/plays/{play_id}")
async def get_play(
    play_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    play = (await db.execute(
        select(Play).where(Play.id == play_id, Play.user_id == user.id)
    )).scalar_one_or_none()
    if not play:
        raise HTTPException(status_code=404, detail="Not found")
    return {"success": True, "data": _serialize_play(play)}


@router.put("/api/plays/{play_id}")
async def update_play(
    play_id: int,
    body: _PlayUpdateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    play = (await db.execute(
        select(Play).where(Play.id == play_id, Play.user_id == user.id)
    )).scalar_one_or_none()
    if not play:
        raise HTTPException(status_code=404, detail="Not found")

    data = body.model_dump(exclude_unset=True)
    for key in ("name", "description", "offense_template", "defense_template",
                "ball_holder_id"):
        if key in data:
            setattr(play, key, data[key])
    if "players" in data:
        play.players_json = data["players"] or []
    if "actions" in data:
        play.actions_json = data["actions"] or []
    play.updated_at = datetime.utcnow().isoformat()
    await db.flush()
    return {"success": True, "data": _serialize_play(play)}


@router.delete("/api/plays/{play_id}")
async def delete_play(
    play_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    existing = (await db.execute(
        select(Play.id).where(Play.id == play_id, Play.user_id == user.id)
    )).scalar_one_or_none()
    if not existing:
        raise HTTPException(status_code=404, detail="Not found")
    await db.execute(delete(Play).where(Play.id == play_id))
    await db.flush()
    return {"success": True}


# ---------------------------------------------------------------------------
# Share
# ---------------------------------------------------------------------------

@router.post("/api/plays/share", status_code=201)
async def share_play(
    body: dict,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Snapshot any play data (doesn't have to be a saved Play row) under a
    short URL-safe token. The token is returned alongside a fully-qualified
    URL so the JS can copy it to clipboard."""
    if not body:
        raise HTTPException(status_code=400, detail="Play data required")

    token = secrets.token_urlsafe(8)  # ~11 chars, URL-safe
    snapshot = PlayShare(
        token=token,
        play_json=body,
        user_id=user.id,
        created_at=datetime.utcnow().isoformat(),
    )
    db.add(snapshot)
    await db.flush()

    base_url = str(request.base_url).rstrip("/")
    return {"token": token, "url": f"{base_url}/play/{token}"}


@router.get("/play/{token}")
async def public_play(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Public, read-only — no auth. Returns the JSON snapshot or 404."""
    row = (await db.execute(
        select(PlayShare.play_json).where(PlayShare.token == token)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return {"play_data": row}
