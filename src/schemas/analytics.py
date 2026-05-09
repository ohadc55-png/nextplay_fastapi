"""Analytics ingest schemas — page views + onboarding events."""

from __future__ import annotations

from pydantic import BaseModel


class PageViewCreate(BaseModel):
    """`/api/tracking/page-views` — fired by the SPA on navigation."""

    session_id: str
    page_path: str
    page_section: str
    duration_seconds: int | None = 0
    entered_at: str  # ISO timestamp from the client
    exited_at: str | None = None


class OnboardingEventCreate(BaseModel):
    """`/api/onboarding/events` — first-use milestone fired by the SPA."""

    event: str  # e.g. "created_first_play", "uploaded_video"


__all__ = ["PageViewCreate", "OnboardingEventCreate"]
