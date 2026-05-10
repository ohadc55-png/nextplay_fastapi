"""Analytics ingest router — page views + onboarding events.

Mirrors the v1.0-flask `tracking_bp` blueprint. Both endpoints accept a
JSON body, write a row, and return 200. They're called by the SPA via
fetch on every navigation / first-use milestone, so the router must be:
- Cheap (no joins, single INSERT)
- Authenticated (no anonymous traffic)
- Idempotent for onboarding events (UNIQUE prevents duplicates)
"""

from __future__ import annotations

import logging
from datetime import UTC

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.database import get_db
from src.models.analytics import OnboardingEvent, PageView
from src.models.users import User
from src.repositories.analytics_repo import OnboardingEventsRepository, PageViewsRepository
from src.schemas.analytics import OnboardingEventCreate, PageViewCreate
from src.schemas.common import StatusResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["tracking"])


@router.post("/tracking/page-views", response_model=StatusResponse)
async def log_page_view(
    body: PageViewCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Append a navigation event. Powers the admin Activity heatmap."""
    row = PageView(
        user_id=user.id,
        session_id=body.session_id,
        page_path=body.page_path,
        page_section=body.page_section,
        duration_seconds=body.duration_seconds,
        entered_at=body.entered_at,
        exited_at=body.exited_at,
        created_at=body.entered_at,  # log time = entry time (matches v1 behavior)
    )
    await PageViewsRepository(db).create(row)
    return StatusResponse(status="ok")


@router.post("/onboarding/events", response_model=StatusResponse)
async def log_onboarding_event(
    body: OnboardingEventCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a first-use milestone as reached. UNIQUE(user_id, team_id, event)
    keeps it idempotent — a second call for the same milestone is a no-op."""
    if user.active_team_id is None:
        # No active team → nothing to scope the event to. Silently accept;
        # the SPA may fire onboarding events before team setup is complete.
        return StatusResponse(status="ok", detail="no active team")

    repo = OnboardingEventsRepository(db)
    if await repo.has_event(
        user_id=user.id, team_id=user.active_team_id, event=body.event
    ):
        return StatusResponse(status="ok", detail="already recorded")

    from datetime import datetime

    row = OnboardingEvent(
        user_id=user.id,
        team_id=user.active_team_id,
        event=body.event,
        first_seen=datetime.now(UTC).isoformat(),
    )
    await repo.create(row)
    return StatusResponse(status="ok")
