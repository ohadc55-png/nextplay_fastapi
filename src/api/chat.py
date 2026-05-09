"""Chat router — async port of the non-CrewAI portion of `backend/api/chat.py`.

Endpoints:
  POST /api/chat              non-streaming JSON reply
  POST /api/chat-stream       SSE streaming
  POST /api/opening-message   first-message generator (stub for now)

The full CrewAI multi-agent stack lands in later Phase 5 batches —
this batch wires the SPA to a working OpenAI chat surface so coaches
can interact with the new app while we port the agent layer.

File-upload chat (`/api/chat-upload`) is deferred to Phase 7 because it
needs the file processor (PyMuPDF/openpyxl/pandas).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user, require_active_subscription
from src.core.database import get_db
from src.crew.agents import AGENTS
from src.models.users import User
from src.services import chat_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class _ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    session_id: str = Field(min_length=1, max_length=128)
    agent: str | None = None


class _OpeningBody(BaseModel):
    agent: str | None = "gm"
    session_id: str | None = ""
    onboarding: str | None = None


# ---------------------------------------------------------------------------
# Non-streaming chat
# ---------------------------------------------------------------------------

@router.post("/chat")
async def chat(
    body: _ChatBody,
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send a single message, get a JSON reply. Used by mobile clients
    or any frontend path that prefers a single round-trip over SSE."""
    try:
        return await chat_service.send_message(
            db, user=user,
            session_id=body.session_id,
            message=body.message.strip(),
            agent=body.agent,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Streaming chat (SSE)
# ---------------------------------------------------------------------------

@router.post("/chat-stream")
async def chat_stream(
    body: _ChatBody,
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """SSE chat. Each event is `data: {json}\n\n`. The JSON has shape:
      {"t":"chunk","c":"<piece of text>"}   token chunk
      {"t":"done"}                          stream complete
      {"t":"error","message":"..."}         recoverable error

    Headers match v1: `Cache-Control: no-cache` + `X-Accel-Buffering: no`
    so reverse-proxies don't buffer the stream."""
    generator = chat_service.stream_message(
        db, user=user,
        session_id=body.session_id,
        message=body.message.strip(),
        agent=body.agent,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Opening message (stub until agent personalities land)
# ---------------------------------------------------------------------------

@router.get("/agents")
async def list_agents(_user: User = Depends(get_current_user)) -> dict:
    """Return the staff card for the SPA's agent picker. Order is the v1
    display order (gm first, then specialists)."""
    order = ["gm", "scout", "analytics", "tactics", "training"]
    return {
        "agents": [
            {"key": key, **AGENTS[key]} for key in order if key in AGENTS
        ],
    }


@router.post("/opening-message")
async def opening_message(
    body: _OpeningBody,
    user: User = Depends(require_active_subscription),
) -> dict:
    """First message shown when the user opens chat. v1 generates a
    personalized greeting via the GM agent's prompt; for now we emit a
    static greeting so the chat UI bootstraps cleanly. The agent-driven
    version lands with the agent personalities batch."""
    name = (user.display_name or user.email.split("@")[0]).strip() or "Coach"
    return {
        "response": (
            f"Hey {name} — let's talk basketball. What's on your mind today? "
            "I can help with practice plans, game strategy, scouting, or "
            "personalized drills. Just ask."
        ),
        "agent_used": (body.agent or "gm"),
        "session_id": body.session_id or "",
    }
