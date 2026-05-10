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

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
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
    # "fast" (default, 2-5s) or "full" (CrewAI multi-step, 30-60s).
    mode: str = "fast"
    # "scouting" → Brad walks the coach through un-profiled players
    # (used when /chat?onboarding=scouting). Anything else → ignored.
    onboarding: str | None = None


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
    background: BackgroundTasks,
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send a single message, get a JSON reply. Used by mobile clients
    or any frontend path that prefers a single round-trip over SSE."""
    onboarding = body.onboarding if body.onboarding in ("scouting",) else None
    try:
        return await chat_service.send_message(
            db, user=user,
            session_id=body.session_id,
            message=body.message.strip(),
            agent=body.agent,
            background=background,
            mode=body.mode,
            onboarding_mode=onboarding,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Streaming chat (SSE)
# ---------------------------------------------------------------------------

@router.post("/chat-stream")
async def chat_stream(
    body: _ChatBody,
    background: BackgroundTasks,
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """SSE chat. Each event is `data: {json}\n\n`. The JSON has shape:
      {"t":"chunk","c":"<piece of text>"}   token chunk
      {"t":"done"}                          stream complete
      {"t":"error","message":"..."}         recoverable error

    Headers match v1: `Cache-Control: no-cache` + `X-Accel-Buffering: no`
    so reverse-proxies don't buffer the stream."""
    onboarding = body.onboarding if body.onboarding in ("scouting",) else None
    generator = chat_service.stream_message(
        db, user=user,
        session_id=body.session_id,
        message=body.message.strip(),
        agent=body.agent,
        background=background,
        onboarding_mode=onboarding,
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


# ---------------------------------------------------------------------------
# File-upload chat (Phase 7 batch 4)
# ---------------------------------------------------------------------------


_MAX_CHAT_UPLOAD_FILES = 3
_MAX_CHAT_UPLOAD_PER_FILE_BYTES = 10 * 1024 * 1024   # 10 MB per file
_MAX_CHAT_UPLOAD_COMBINED_BYTES = 30 * 1024 * 1024   # 30 MB combined


@router.post("/chat-upload")
async def chat_upload(
    background: BackgroundTasks,
    files: list[UploadFile] = File(default_factory=list, alias="file"),
    message: str = Form(default=""),
    session_id: str = Form(default=""),
    agent: str = Form(default=""),
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Handle 1-3 file uploads attached to a chat turn.

    Pipeline:
      1. Reject if no files / too many / oversized
      2. Save each to `data/uploads/<user_id>/<safe_name>` with
         magic-byte validation
      3. Persist an `uploads` row per file
      4. Run vision Stage 1 on images + extract text from data files
      5. Build the enriched message + Stage 2 instruction
      6. Delegate to chat_service.send_message — same tool-loop /
         persistence / memory extraction as a normal chat turn

    Errors at the validation layer surface as 400; LLM errors degrade
    via send_message's friendly fallback (never 500 on the user)."""
    from src.models.uploads import Upload
    from src.services import chat_service
    from src.services.upload_service import save_upload_bytes

    files = [f for f in (files or []) if f and f.filename]
    if not files:
        raise HTTPException(status_code=400, detail="No file")
    if len(files) > _MAX_CHAT_UPLOAD_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files (max {_MAX_CHAT_UPLOAD_FILES})",
        )

    # Read + per-file size check + combined size check.
    raw_files: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        data = await f.read(_MAX_CHAT_UPLOAD_PER_FILE_BYTES + 1)
        if len(data) > _MAX_CHAT_UPLOAD_PER_FILE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"File '{f.filename}' exceeds 10 MB per-file limit",
            )
        total += len(data)
        if total > _MAX_CHAT_UPLOAD_COMBINED_BYTES:
            raise HTTPException(
                status_code=400, detail="Combined file size exceeds 30 MB",
            )
        raw_files.append((f.filename, data))

    # Save to disk + persist Upload rows.
    saved: list[tuple[str, str]] = []  # (filename, abs_filepath)
    for filename, data in raw_files:
        try:
            safe_name, abs_path = await save_upload_bytes(
                user_id=user.id, filename=filename, data=data,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Persist Upload row so /api/uploads/list shows it
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        db.add(Upload(
            user_id=user.id, team_id=user.active_team_id,
            filename=safe_name, filepath=abs_path,
            file_type=ext, category="chat",
            description="",
        ))
        saved.append((safe_name, abs_path))
    await db.flush()

    try:
        return await chat_service.send_chat_with_uploads(
            db, user=user,
            session_id=session_id or "",
            message=message,
            agent=agent or None,
            files=saved,
            background=background,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
