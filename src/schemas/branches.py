"""Pydantic shapes for /org/api/branches/* (Phase 1.3)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class BranchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    region_id: int | None = None  # null = unassigned to a region (allowed)


class BranchUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    region_id: int | None = None


class BranchOut(ORMModel):
    id: int
    organization_id: int
    region_id: int | None = None
    name: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Phase-1 follow-up: enrichment populated by the list endpoint.
    team_count: int | None = None
    player_count: int | None = None


__all__ = ["BranchCreate", "BranchOut", "BranchUpdate"]
