"""Teams CRUD + active team switch + team-profile save.

Async port of `backend/api/teams.py`. The team-profile save endpoint
upserts the row keyed on the active team — same shape v1 has.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.database import get_db
from src.models.teams import TeamProfile
from src.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["teams"])


def _serialize_team(t: TeamProfile) -> dict:
    return {
        "id": t.id, "user_id": t.user_id,
        "team_name": t.team_name,
        "league": t.league or "", "division": t.division or "",
        "play_style": t.play_style or "",
        "strengths": t.strengths or "", "weaknesses": t.weaknesses or "",
        "notes": t.notes or "",
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "extra_storage_gb": t.extra_storage_gb or 0,
    }


@router.get("/teams")
async def list_teams(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = list((await db.execute(
        select(TeamProfile)
        .where(TeamProfile.user_id == user.id)
        .order_by(TeamProfile.id)
    )).scalars().all())
    return [_serialize_team(t) for t in rows]


class _TeamCreateBody(BaseModel):
    team_name: str = Field(min_length=1, max_length=255)
    league: str | None = ""
    division: str | None = ""


@router.post("/teams", status_code=201)
async def create_team(
    body: _TeamCreateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a team for the current user. If the user has no active team
    yet, this team becomes active."""
    name = body.team_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="team_name is required")
    t = TeamProfile(
        user_id=user.id, team_name=name,
        league=body.league or "", division=body.division or "",
    )
    db.add(t)
    await db.flush()

    # Auto-select if this is the user's first team
    if user.active_team_id is None:
        user.active_team_id = t.id
        await db.flush()
    return _serialize_team(t)


@router.post("/teams/{team_id}/switch")
async def switch_team(
    team_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set this team as the current active. The team must belong to the user."""
    t = (await db.execute(
        select(TeamProfile).where(
            TeamProfile.id == team_id, TeamProfile.user_id == user.id
        )
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Team not found")
    user.active_team_id = team_id
    await db.flush()
    return {"ok": True, "active_team_id": team_id}


@router.delete("/teams/{team_id}")
async def delete_team(
    team_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a team. Cannot delete the only team a user has (v1 behavior)."""
    teams = list((await db.execute(
        select(TeamProfile).where(TeamProfile.user_id == user.id)
    )).scalars().all())
    target = next((t for t in teams if t.id == team_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Team not found")
    if len(teams) <= 1:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete your only team — create another first.",
        )

    await db.execute(delete(TeamProfile).where(TeamProfile.id == team_id))

    # If the deleted team was active, fall back to another one.
    if user.active_team_id == team_id:
        user.active_team_id = next(t.id for t in teams if t.id != team_id)
    await db.flush()
    return {"ok": True, "active_team_id": user.active_team_id}


class _TeamProfileBody(BaseModel):
    team_name: str | None = None
    league: str | None = None
    division: str | None = None
    play_style: str | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    notes: str | None = None


@router.post("/team-profile")
async def save_team_profile(
    body: _TeamProfileBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Upsert the active team's profile fields. Requires an active team."""
    if user.active_team_id is None:
        raise HTTPException(status_code=400, detail="no active team")

    t = (await db.execute(
        select(TeamProfile).where(
            TeamProfile.id == user.active_team_id,
            TeamProfile.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Active team not found")

    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(t, key, value)
    t.updated_at = datetime.utcnow()
    await db.flush()
    return _serialize_team(t)
