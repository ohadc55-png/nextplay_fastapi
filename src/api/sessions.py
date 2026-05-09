"""Chat session/history endpoints — async port of `backend/api/sessions.py`.

Three endpoints:
  GET /api/history/{session_id}    messages in a session (tenant-checked)
  GET /api/search                  search across the user's conversations
  GET /api/new-session             mint a fresh session_id (no DB write)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.database import get_db
from src.models.conversations import Conversation
from src.models.users import User

router = APIRouter(prefix="/api", tags=["sessions"])


def _serialize_message(c: Conversation) -> dict:
    return {
        "id": c.id, "session_id": c.session_id,
        "role": c.role, "content": c.content,
        "agent_used": c.agent_used,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/history/{session_id}")
async def session_messages(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Messages in a session, oldest-first. Tenant-checked: a coach can't
    fetch another coach's session even with the right session_id."""
    stmt = (
        select(Conversation)
        .where(
            Conversation.session_id == session_id,
            Conversation.user_id == user.id,
        )
        .order_by(Conversation.created_at)
    )
    if user.active_team_id is not None:
        stmt = stmt.where(Conversation.team_id == user.active_team_id)
    rows = list((await db.execute(stmt)).scalars().all())
    return [_serialize_message(c) for c in rows]


@router.get("/search")
async def search_conversations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    q: str = Query(default="", max_length=200),
) -> list[dict]:
    """Substring match in `content`. Empty `q` returns []. Tenant-scoped to
    the active team."""
    if not q.strip():
        return []
    like = f"%{q.lower()}%"
    from sqlalchemy import func
    stmt = (
        select(Conversation)
        .where(
            Conversation.user_id == user.id,
            func.lower(Conversation.content).like(like),
        )
        .order_by(Conversation.created_at.desc().nulls_last())
        .limit(50)
    )
    if user.active_team_id is not None:
        stmt = stmt.where(Conversation.team_id == user.active_team_id)
    rows = list((await db.execute(stmt)).scalars().all())
    return [_serialize_message(c) for c in rows]


@router.get("/new-session")
async def new_session(
    user: User = Depends(get_current_user),
) -> dict:
    """Mint a fresh session_id. No DB write — the first message in the
    session creates the conversation rows. Format mirrors v1 exactly so
    the chat UI's deep-link handling stays compatible."""
    sid = (
        datetime.utcnow().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    )
    return {"session_id": sid}
