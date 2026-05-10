"""Players CRUD + metrics + bulk add.

Async port of `backend/api/players.py`. File-based bulk import (CSV / XLSX
parsing) is deferred to Phase 7 alongside the file processor; the
template downloads + photo upload S3 plumbing land in Phase 6/7. The
JSON `bulk` endpoint stays here since it's pure DB.
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
from src.models.players import Player, PlayerMetric
from src.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["players"])


def _serialize_player(p: Player) -> dict:
    return {
        "id": p.id, "user_id": p.user_id, "team_id": p.team_id,
        "name": p.name, "number": p.number,
        "position": p.position or "", "height": p.height or "",
        "weight": p.weight or "", "age": p.age,
        "strengths": p.strengths or "", "weaknesses": p.weaknesses or "",
        "notes": p.notes or "",
        "dominant_hand": p.dominant_hand or "",
        "active": bool(p.active),
        "photo_url": p.photo_url or "",
        "scout_summary": p.scout_summary or "",
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _require_team(user: User) -> int:
    if user.active_team_id is None:
        raise HTTPException(status_code=400, detail="no active team")
    return user.active_team_id


# ---------------------------------------------------------------------------
# Single player CRUD
# ---------------------------------------------------------------------------

class _PlayerCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    number: int | None = None
    position: str | None = ""
    height: str | None = ""
    weight: str | None = ""
    age: int | None = None
    strengths: str | None = ""
    weaknesses: str | None = ""
    notes: str | None = ""
    dominant_hand: str | None = ""


class _PlayerUpdateBody(BaseModel):
    name: str | None = None
    number: int | None = None
    position: str | None = None
    height: str | None = None
    weight: str | None = None
    age: int | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    notes: str | None = None
    dominant_hand: str | None = None
    active: bool | None = None


@router.post("/player", status_code=201)
async def add_player(
    body: _PlayerCreateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    team_id = _require_team(user)
    p = Player(
        user_id=user.id, team_id=team_id, name=body.name.strip(),
        number=body.number, position=body.position or "",
        height=body.height or "", weight=body.weight or "",
        age=body.age, strengths=body.strengths or "",
        weaknesses=body.weaknesses or "", notes=body.notes or "",
        dominant_hand=body.dominant_hand or "",
        active=True,
    )
    db.add(p)
    await db.flush()
    return _serialize_player(p)


@router.put("/player/{player_id}")
async def update_player(
    player_id: int,
    body: _PlayerUpdateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    p = (await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == user.id)
    )).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Player not found")

    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(p, key, value)
    await db.flush()
    return _serialize_player(p)


@router.delete("/player/{player_id}")
async def delete_player(
    player_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft delete: flip active=False (matches v1 — preserves history,
    stats, notebook references)."""
    existing = (await db.execute(
        select(Player.id).where(Player.id == player_id, Player.user_id == user.id)
    )).scalar_one_or_none()
    if not existing:
        raise HTTPException(status_code=404, detail="Player not found")
    await db.execute(
        update(Player).where(Player.id == player_id).values(active=False)
    )
    await db.flush()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class _MetricsBody(BaseModel):
    metrics: dict = Field(default_factory=dict)


@router.post("/player/{player_id}/metrics")
async def save_player_metrics(
    player_id: int,
    body: _MetricsBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Upsert PlayerMetric (UNIQUE on player_id). Sets metrics_filled_at
    on the player row so the UI can show 'metrics complete' badges."""
    p = (await db.execute(
        select(Player).where(Player.id == player_id, Player.user_id == user.id)
    )).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Player not found")

    row = (await db.execute(
        select(PlayerMetric).where(PlayerMetric.player_id == player_id)
    )).scalar_one_or_none()
    if row:
        row.metrics_json = body.metrics or {}
        row.updated_at = datetime.utcnow().isoformat()
    else:
        row = PlayerMetric(
            player_id=player_id, user_id=user.id, team_id=p.team_id,
            metrics_json=body.metrics or {},
            updated_at=datetime.utcnow().isoformat(),
        )
        db.add(row)

    p.metrics_filled_at = datetime.utcnow()
    await db.flush()
    return {"ok": True, "metrics": row.metrics_json}


# ---------------------------------------------------------------------------
# Bulk add (JSON shape — CSV/XLSX file ingest deferred to Phase 7)
# ---------------------------------------------------------------------------

class _BulkAddBody(BaseModel):
    players: list[dict] = Field(default_factory=list)


@router.post("/players/bulk")
async def bulk_add_players(
    body: _BulkAddBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Insert many players at once. Skips empty-name rows. Returns the
    number actually inserted so the UI can show 'imported X / skipped Y'."""
    team_id = _require_team(user)
    inserted = 0
    skipped = 0
    for raw in body.players:
        name = (raw.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        try:
            number = int(raw.get("number") or 0) or None
        except (TypeError, ValueError):
            number = None
        try:
            age = int(raw.get("age") or 0) or None
        except (TypeError, ValueError):
            age = None
        db.add(Player(
            user_id=user.id, team_id=team_id, name=name,
            number=number, position=(raw.get("position") or "").strip(),
            height=(raw.get("height") or "").strip(),
            weight=(raw.get("weight") or "").strip(),
            age=age,
            strengths=(raw.get("strengths") or "").strip(),
            weaknesses=(raw.get("weaknesses") or "").strip(),
            notes=(raw.get("notes") or "").strip(),
            active=True,
        ))
        inserted += 1
    await db.flush()
    return {"ok": True, "inserted": inserted, "skipped": skipped}
