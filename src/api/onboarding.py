"""Onboarding router — async port of `backend/api/onboarding.py`.

Endpoints (mirroring v1's surface 1:1 — frontend JS hits these by URL,
so the paths must not drift):

  GET  /api/onboarding-status                  Roadmap widget on /main
  GET  /api/onboarding/players-status          Brad's "who's done / next" data
  POST /api/onboarding/extract-player-skills   LLM-extract metrics from chat
  POST /api/onboarding/mark-event              First-use event (idempotent)

All four require an authenticated coach with an active team. The
heavy lifting lives in `src/services/onboarding_service.py`; this
file is just the FastAPI wiring + shape validation.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.database import get_db
from src.models.users import User
from src.services import onboarding_service as obs

logger = logging.getLogger(__name__)

router = APIRouter(tags=["onboarding"])


# Cache control for status endpoints — the widget must reflect actions
# the coach JUST performed (uploaded a file, added a player). Identical
# headers to v1 so the SPA's revalidation behavior is unchanged.
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class _ExtractBody(BaseModel):
    player_id: int = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=4000)


class _EventBody(BaseModel):
    event: str = Field(..., min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# GET /api/onboarding-status — drives the home-page roadmap widget
# ---------------------------------------------------------------------------


@router.get("/api/onboarding-status")
async def onboarding_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return the coach's onboarding completion status. Always 200 even
    when the coach has no active team yet (the widget renders `0/12`
    and the JS shows the right empty state)."""
    if user.active_team_id is None:
        return JSONResponse(
            {"all_done": False, "total_done": 0, "total_items": 12, "stages": []},
            headers=_NO_CACHE,
        )
    try:
        payload = await obs.compute_onboarding_status(
            db, user_id=user.id, team_id=user.active_team_id,
        )
    except Exception:
        logger.exception("[onboarding] status computation failed")
        raise HTTPException(
            status_code=500, detail="Failed to compute onboarding status",
        )
    return JSONResponse(payload, headers=_NO_CACHE)


# ---------------------------------------------------------------------------
# GET /api/onboarding/players-status — Brad's roster snapshot
# ---------------------------------------------------------------------------


@router.get("/api/onboarding/players-status")
async def onboarding_players_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return `{profiled, pending, next}` for Brad's walkthrough UI.
    Empty lists when there's no active team — matches v1 contract."""
    if user.active_team_id is None:
        return {"profiled": [], "pending": [], "next": None}
    return await obs.players_profiling_status(
        db, user_id=user.id, team_id=user.active_team_id,
    )


# ---------------------------------------------------------------------------
# POST /api/onboarding/extract-player-skills — LLM extraction
# ---------------------------------------------------------------------------


@router.post("/api/onboarding/extract-player-skills")
async def extract_player_skills(
    body: _ExtractBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """LLM-extract player metrics + strengths/weaknesses from the
    coach's free-text description. Update the player row + persist
    merged metrics. Returns the parsed result."""
    if user.active_team_id is None:
        raise HTTPException(status_code=400, detail="No active team")

    description = body.description.strip()
    if not description:
        raise HTTPException(
            status_code=400, detail="player_id and description are required",
        )

    result = await obs.extract_player_skills(
        db,
        user_id=user.id,
        team_id=user.active_team_id,
        player_id=body.player_id,
        description=description,
        set_metrics_filled_at=True,
    )

    if not result.get("success"):
        err = result.get("error")
        # v1 maps Player-not-found → 404, extraction LLM failure → 502,
        # everything else → 400. Keep the same shape so SPA error
        # branches stay consistent.
        if err == "Player not found":
            raise HTTPException(status_code=404, detail=err)
        status = 502 if err == "extraction_failed" else 400
        raise HTTPException(status_code=status, detail={
            "error": err, "detail": result.get("detail"),
        })

    return result


# ---------------------------------------------------------------------------
# POST /api/onboarding/mark-event — record first-use (idempotent)
# ---------------------------------------------------------------------------


@router.post("/api/onboarding/mark-event")
async def mark_event(
    body: _EventBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record a first-use event for the home roadmap. The set of
    allowed events is intentionally narrow — see
    `onboarding_service.ALLOWED_EVENTS`. Unknown events return 400
    (mirrors v1)."""
    if user.active_team_id is None:
        raise HTTPException(status_code=400, detail="No active team")

    event = body.event.strip()
    if event not in obs.ALLOWED_EVENTS:
        raise HTTPException(status_code=400, detail="Unknown event")

    ok = await obs.mark_event(
        db, user_id=user.id, team_id=user.active_team_id, event=event,
    )
    return {"ok": ok}
