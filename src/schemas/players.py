"""Player + metrics + game-stats schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class PlayerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    number: int | None = None
    position: str | None = None
    height: str | None = None
    weight: str | None = None
    age: int | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    notes: str | None = None
    dominant_hand: str | None = ""


class PlayerUpdate(BaseModel):
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
    photo_url: str | None = None


class PlayerResponse(ORMModel):
    id: int
    user_id: int | None = None
    team_id: int | None = None
    name: str
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
    photo_url: str | None = None
    scout_summary: str | None = None


class PlayerMetricsUpdate(BaseModel):
    """Coaches save the radar-chart metrics object freely; we don't enforce
    keys. Mirrors v1.0-flask which stores metrics_json as freeform JSON."""

    metrics: dict


class PlayerMetricsResponse(ORMModel):
    player_id: int
    metrics_json: dict | None = None
    updated_at: str | None = None


class PlayerGameStatCreate(BaseModel):
    player_id: int
    game_date: str  # ISO date (YYYY-MM-DD)
    opponent: str | None = ""
    minutes: int | None = 0
    points: int | None = 0
    fgm: int | None = 0
    fga: int | None = 0
    three_pm: int | None = 0
    three_pa: int | None = 0
    ftm: int | None = 0
    fta: int | None = 0
    oreb: int | None = 0
    dreb: int | None = 0
    reb: int | None = 0
    ast: int | None = 0
    stl: int | None = 0
    blk: int | None = 0
    turnovers: int | None = 0
    pf: int | None = 0
    plus_minus: int | None = 0
    notebook_entry_id: int | None = None


class PlayerGameStatResponse(PlayerGameStatCreate, ORMModel):
    id: int
    user_id: int
    team_id: int
    created_at: str | None = None


__all__ = [
    "PlayerCreate",
    "PlayerUpdate",
    "PlayerResponse",
    "PlayerMetricsUpdate",
    "PlayerMetricsResponse",
    "PlayerGameStatCreate",
    "PlayerGameStatResponse",
]
