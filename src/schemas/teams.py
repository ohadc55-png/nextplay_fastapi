"""Team profile schemas — `/api/teams/*`."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class TeamCreate(BaseModel):
    team_name: str = Field(min_length=1, max_length=255)
    league: str | None = None
    division: str | None = None


class TeamUpdate(BaseModel):
    team_name: str | None = None
    league: str | None = None
    division: str | None = None
    play_style: str | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    notes: str | None = None


class TeamResponse(ORMModel):
    id: int
    user_id: int | None = None
    team_name: str
    league: str | None = None
    division: str | None = None
    play_style: str | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    notes: str | None = None
    extra_storage_gb: int | None = 0


__all__ = ["TeamCreate", "TeamResponse", "TeamUpdate"]
