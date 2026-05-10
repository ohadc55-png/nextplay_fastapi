"""Coach personalization schemas — preferences + feedback."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class CoachPreferenceUpdate(BaseModel):
    """`/api/coach/settings` — every field optional (PATCH)."""

    preferred_language: str | None = None
    detail_level: str | None = None
    focus_areas: str | None = None
    avoid_topics: str | None = None
    practice_duration: int | None = None
    coaching_style: str | None = None
    custom_notes: str | None = None


class CoachPreferenceResponse(ORMModel):
    preferred_language: str | None = "en"
    detail_level: str | None = "medium"
    focus_areas: str | None = ""
    avoid_topics: str | None = ""
    practice_duration: int | None = 90
    coaching_style: str | None = ""
    custom_notes: str | None = ""


class FeedbackCreate(BaseModel):
    """`/api/feedback` — coach rates an agent response."""

    agent_key: str = Field(min_length=1, max_length=64)
    message_content: str
    response_content: str
    rating: int  # +1 / -1
    comment: str | None = ""


class FeedbackResponse(ORMModel):
    id: int
    user_id: int
    team_id: int | None = None
    agent_key: str
    message_content: str
    response_content: str
    rating: int
    comment: str | None = None
    created_at: str | None = None


__all__ = [
    "CoachPreferenceResponse",
    "CoachPreferenceUpdate",
    "FeedbackCreate",
    "FeedbackResponse",
]
