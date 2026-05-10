"""Chat / conversations schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class ChatRequest(BaseModel):
    """`/api/chat` and `/api/chat-stream` body."""

    message: str = Field(min_length=1)
    session_id: str | None = None  # server-generated if missing
    agent: str | None = None  # explicit agent override (rare)


class OpeningMessageRequest(BaseModel):
    """`/api/opening-message` — generate a personalized greeting."""

    session_id: str | None = None


class ChatChunk(BaseModel):
    """One SSE chunk emitted during streaming. Matches v1.0-flask SSE shape."""

    type: str  # "delta" | "agent" | "done" | "error"
    content: str = ""
    agent: str | None = None
    metadata: dict | None = None


class ConversationMessage(ORMModel):
    """One row of chat history."""

    id: int
    session_id: str
    role: str
    content: str
    agent_used: str | None = None
    created_at: str | None = None


class SessionSummaryResponse(BaseModel):
    """Summary of a session as it appears on the history page."""

    session_id: str
    first_message: str
    message_count: int
    started_at: str | None = None
    last_message_at: str | None = None


__all__ = [
    "ChatChunk",
    "ChatRequest",
    "ConversationMessage",
    "OpeningMessageRequest",
    "SessionSummaryResponse",
]
