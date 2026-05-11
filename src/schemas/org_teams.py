"""Pydantic shapes for /org/api/teams/* (Phase 1.5 — Team Management)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class OrgTeamCreate(BaseModel):
    """A new team inside the active org. branch_id is optional but recommended;
    when set, the team is scoped to that branch (branch_manager + coaches at
    that branch will see it). team_name is required."""

    team_name: str = Field(min_length=1, max_length=200)
    league: str | None = None
    division: str | None = None
    play_style: str | None = None
    notes: str | None = None
    branch_id: int | None = None


class OrgTeamUpdate(BaseModel):
    team_name: str | None = Field(default=None, min_length=1, max_length=200)
    league: str | None = None
    division: str | None = None
    play_style: str | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    notes: str | None = None
    branch_id: int | None = None


class OrgTeamOut(ORMModel):
    id: int
    organization_id: int | None = None
    branch_id: int | None = None
    user_id: int | None = None  # primary coach (single-owner; multi-coach deferred)
    team_name: str
    league: str | None = None
    division: str | None = None
    play_style: str | None = None
    notes: str | None = None
    updated_at: datetime | None = None


class CoachAssignRequest(BaseModel):
    """POST /org/api/teams/{id}/coaches — grant the user `role=coach` on
    the team's org and set team_profile.user_id (single-owner)."""

    user_id: int


__all__ = [
    "CoachAssignRequest",
    "OrgTeamCreate",
    "OrgTeamOut",
    "OrgTeamUpdate",
]
