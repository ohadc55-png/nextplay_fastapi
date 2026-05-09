"""Coach notebook schemas — entries, attendance, M-M player tagging."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class NotebookEntryCreate(BaseModel):
    entry_type: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    entry_date: str  # YYYY-MM-DD
    content_json: dict = Field(default_factory=dict)
    player_id: int | None = None
    source: str | None = "manual"
    tags_json: list = Field(default_factory=list)


class NotebookEntryUpdate(BaseModel):
    title: str | None = None
    entry_date: str | None = None
    content_json: dict | None = None
    player_id: int | None = None
    tags_json: list | None = None


class NotebookEntryResponse(ORMModel):
    id: int
    user_id: int
    team_id: int
    entry_type: str
    title: str
    entry_date: str
    content_json: dict | None = None
    player_id: int | None = None
    source: str | None = None
    tags_json: list | None = None
    created_at: str | None = None
    updated_at: str | None = None


class NotebookAttendanceCreate(BaseModel):
    entry_id: int
    player_id: int
    status: str = "present"  # present | absent | late | excused
    note: str | None = ""


class NotebookAttendanceResponse(ORMModel):
    id: int
    entry_id: int
    player_id: int
    status: str | None = None
    note: str | None = None


class NotebookFormatForSaveRequest(BaseModel):
    """`/api/notebook/format-for-save` — paste raw chat content, agent
    formats it into the structured schema for the chosen entry_type."""

    entry_type: str
    raw_content: str


__all__ = [
    "NotebookEntryCreate",
    "NotebookEntryUpdate",
    "NotebookEntryResponse",
    "NotebookAttendanceCreate",
    "NotebookAttendanceResponse",
    "NotebookFormatForSaveRequest",
]
