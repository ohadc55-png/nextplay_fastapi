"""Play creator + share-link schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class PlayCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = ""
    offense_template: str | None = "empty"
    defense_template: str | None = "none"
    players_json: list = Field(default_factory=list)
    actions_json: list = Field(default_factory=list)
    ball_holder_id: str | None = None


class PlayUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    offense_template: str | None = None
    defense_template: str | None = None
    players_json: list | None = None
    actions_json: list | None = None
    ball_holder_id: str | None = None


class PlayResponse(ORMModel):
    id: int
    user_id: int | None = None
    team_id: int | None = None
    name: str
    description: str | None = None
    offense_template: str | None = None
    defense_template: str | None = None
    players_json: list | None = None
    actions_json: list | None = None
    ball_holder_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class PlayShareCreate(BaseModel):
    """Snapshot a play to a public-readable share token."""

    play_id: int


class PlayShareResponse(ORMModel):
    id: int
    user_id: int | None = None
    token: str
    play_json: dict | None = None
    created_at: str | None = None


__all__ = ["PlayCreate", "PlayResponse", "PlayShareCreate", "PlayShareResponse", "PlayUpdate"]
