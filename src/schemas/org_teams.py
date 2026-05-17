"""Pydantic shapes for /org/api/teams/* (Phase 1.5 — Team Management)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class OrgTeamCreate(BaseModel):
    """A new team inside the active org. region_id is optional but recommended
    for the new hierarchy (Org → Region → Team, with Program as an
    independent axis). branch_id stays accepted for back-compat with Phase
    0; setting BOTH region_id + branch_id is allowed.

    program_id (Phase 12): a direct attribute on the team. For PM the
    server forces this to their own program regardless of input — the
    field is here so org_admin can assign teams to any program."""

    team_name: str = Field(min_length=1, max_length=200)
    league: str | None = None
    division: str | None = None
    play_style: str | None = None
    notes: str | None = None
    region_id: int | None = None
    branch_id: int | None = None
    program_id: int | None = None


class OrgTeamUpdate(BaseModel):
    team_name: str | None = Field(default=None, min_length=1, max_length=200)
    league: str | None = None
    division: str | None = None
    play_style: str | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    notes: str | None = None
    region_id: int | None = None
    branch_id: int | None = None
    program_id: int | None = None


class OrgTeamOut(ORMModel):
    id: int
    organization_id: int | None = None
    branch_id: int | None = None
    region_id: int | None = None
    user_id: int | None = None  # primary coach (single-owner; multi-coach deferred)
    team_name: str
    league: str | None = None
    division: str | None = None
    play_style: str | None = None
    notes: str | None = None
    updated_at: datetime | None = None
    # Enriched in the list endpoint so the table can render without
    # extra round-trips. None for routes (create/get) that don't enrich.
    region_name: str | None = None
    program_id: int | None = None
    program_name: str | None = None
    coach_display_name: str | None = None
    coach_email: str | None = None


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
