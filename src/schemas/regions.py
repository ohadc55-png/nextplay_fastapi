"""Pydantic shapes for /org/api/regions/* (Phase 1.3)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class RegionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    program_id: int | None = None


class RegionUpdate(BaseModel):
    """All fields optional — PATCH semantics."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    program_id: int | None = None


class RegionOut(ORMModel):
    id: int
    organization_id: int
    name: str
    program_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Phase-1 follow-up: enrichment fields populated by the list endpoint.
    # Optional so single-region routes (CREATE/GET/PATCH) keep their lean shape.
    manager_name: str | None = None
    program_name: str | None = None
    branch_count: int | None = None
    team_count: int | None = None
    coach_count: int | None = None
    player_count: int | None = None


__all__ = ["RegionCreate", "RegionOut", "RegionUpdate"]
