"""Chat service — minimal direct-OpenAI port (Phase 5 batch 2).

This module wires the chat endpoints to the OpenAI API directly. The
full v1 stack (CrewAI multi-agent + 3-layer routing + ChromaDB RAG +
research pipeline + memory extraction + vision pipeline) lands in
later Phase 5 batches. For now the SPA gets a working chat surface
while we port those pieces incrementally.

What this batch does:
  - Loads the recent conversation history for a session
  - Sends [system] + history + new message to gpt-4o-mini
  - Streams the response (SSE format matches v1 byte-for-byte)
  - Persists user + assistant messages to `conversations`
  - Logs cost via `log_response` so admin /api/api-costs stays accurate

What's deferred (Phase 5 follow-on batches):
  - Agent personalities (Brad/Hunter/Nexus/Vance/Williams)
  - 3-layer routing (deterministic shortcuts → semantic match → LLM)
  - ChromaDB RAG context injection
  - Memory extractor (smart team-scoping rules)
  - Web research (8-stage pipeline)
  - Vision two-stage pipeline (GPT-4o Vision describe → specialist)
  - File-upload chat (Phase 7 file processor)
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from openai import APIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.crew.llm import get_client, log_api_usage, log_response
from src.models.conversations import Conversation
from src.models.users import User

logger = logging.getLogger(__name__)


# Default system prompt until the agent-routing batch lands. Mirrors v1's
# generic GM tone — "you are a helpful basketball coaching assistant" —
# rather than putting words in any specific agent's mouth.
_DEFAULT_SYSTEM_PROMPT = (
    "You are NEXTPLAY, an AI basketball coaching assistant. Reply in plain "
    "text (no markdown). Keep answers focused and actionable. If the user "
    "asks for a play, drill, or scouting report, give specific, concrete "
    "advice. Never invent player names or stats — if you don't know, say so."
)

# Cap on history we send to the LLM. v1 uses 12; matching it.
_HISTORY_LIMIT = 12

_MODEL = "gpt-4o-mini"


async def _load_history(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: int,
    team_id: int | None,
    limit: int = _HISTORY_LIMIT,
) -> list[dict]:
    """Pull the most recent N messages for a session, oldest-first.
    Returns OpenAI-shape dicts (`{"role", "content"}`)."""
    stmt = (
        select(Conversation)
        .where(
            Conversation.session_id == session_id,
            Conversation.user_id == user_id,
        )
        .order_by(Conversation.created_at.desc())
        .limit(limit)
    )
    if team_id is not None:
        stmt = stmt.where(Conversation.team_id == team_id)
    rows = list((await db.execute(stmt)).scalars().all())
    rows.reverse()  # back to oldest-first
    out: list[dict] = []
    for c in rows:
        # Coerce assistant-from-an-agent rows back to plain "assistant" so
        # the OpenAI API doesn't reject them.
        role = c.role if c.role in ("user", "assistant", "system") else "assistant"
        out.append({"role": role, "content": c.content or ""})
    return out


def _build_messages(
    history: list[dict], user_message: str
) -> list[dict]:
    """Compose the [system, ...history, user] payload."""
    return [
        {"role": "system", "content": _DEFAULT_SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": user_message},
    ]


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------

async def send_message(
    db: AsyncSession,
    *,
    user: User,
    session_id: str,
    message: str,
    agent: str | None = None,
) -> dict:
    """Send a single user message, return the assistant reply.

    Persists both messages to `conversations` and logs cost. Errors are
    caught and surfaced as a friendly response — the chat must never
    500 on the user."""
    if not message:
        raise ValueError("Message is required")
    if len(message) > 5000:
        raise ValueError("Message too long (max 5000 characters)")

    # Save user message FIRST so the row exists even if the LLM call fails.
    db.add(Conversation(
        session_id=session_id,
        user_id=user.id,
        team_id=user.active_team_id,
        role="user",
        content=message,
    ))
    await db.flush()

    history = await _load_history(
        db, session_id=session_id, user_id=user.id, team_id=user.active_team_id,
    )

    agent_key = (agent or "default").strip()
    response_text = ""
    try:
        client = get_client()
        # Trim the history we just wrote off the tail — the user msg is
        # already in `messages` below.
        resp = await client.chat.completions.create(
            model=_MODEL,
            messages=_build_messages(history[:-1], message),
            temperature=0.7,
        )
        response_text = (resp.choices[0].message.content or "").strip()
        await log_response(
            db, resp,
            user_id=user.id, team_id=user.active_team_id,
            agent_key=agent_key, endpoint="chat",
        )
    except APIError as e:
        logger.warning("[chat] OpenAI API error: %s", e)
        response_text = "I'm having trouble reaching my coaching brain right now. Try again in a moment."
        agent_key = "error"
    except Exception as e:  # noqa: BLE001
        logger.exception("[chat] unexpected error: %s", e)
        response_text = "Something went wrong. Please try again."
        agent_key = "error"

    db.add(Conversation(
        session_id=session_id,
        user_id=user.id,
        team_id=user.active_team_id,
        role="assistant",
        content=response_text,
        agent_used=agent_key,
    ))
    await db.flush()

    return {
        "response": response_text,
        "session_id": session_id,
        "agent_used": agent_key,
    }


# ---------------------------------------------------------------------------
# Streaming (SSE)
# ---------------------------------------------------------------------------

async def stream_message(
    db: AsyncSession,
    *,
    user: User,
    session_id: str,
    message: str,
    agent: str | None = None,
) -> AsyncIterator[str]:
    """Yield SSE-formatted chunks. Format mirrors v1 byte-for-byte:
        data: {"t":"chunk","c":"..."}\n\n
        data: {"t":"done"}\n\n
    Errors emit `{"t":"error","message":"..."}` and the stream then
    closes — the frontend treats that as a recoverable failure."""
    if not message:
        yield f"data: {json.dumps({'t': 'error', 'message': 'Message is required'})}\n\n"
        return

    db.add(Conversation(
        session_id=session_id, user_id=user.id, team_id=user.active_team_id,
        role="user", content=message,
    ))
    await db.flush()

    history = await _load_history(
        db, session_id=session_id, user_id=user.id, team_id=user.active_team_id,
    )

    agent_key = (agent or "default").strip()
    full_response = ""
    usage_payload: dict | None = None

    try:
        client = get_client()
        # `include_usage` makes OpenAI emit a final usage chunk so we can
        # still log cost on streaming calls (matches v1 §2.9 invariant).
        stream = await client.chat.completions.create(
            model=_MODEL,
            messages=_build_messages(history[:-1], message),
            temperature=0.7,
            stream=True,
            stream_options={"include_usage": True},
        )

        async for event in stream:
            # The final usage chunk has no choices, only .usage
            if not event.choices:
                if getattr(event, "usage", None):
                    usage_payload = {
                        "model": getattr(event, "model", _MODEL),
                        "prompt_tokens": event.usage.prompt_tokens or 0,
                        "completion_tokens": event.usage.completion_tokens or 0,
                    }
                continue
            delta = event.choices[0].delta
            piece = (delta.content or "") if delta else ""
            if piece:
                full_response += piece
                yield f"data: {json.dumps({'t': 'chunk', 'c': piece})}\n\n"

    except APIError as e:
        logger.warning("[chat-stream] OpenAI API error: %s", e)
        yield f"data: {json.dumps({'t': 'error', 'message': 'OpenAI is unreachable. Try again.'})}\n\n"
        agent_key = "error"
    except Exception as e:  # noqa: BLE001
        logger.exception("[chat-stream] unexpected: %s", e)
        yield f"data: {json.dumps({'t': 'error', 'message': 'Something went wrong.'})}\n\n"
        agent_key = "error"

    # Persist the assistant message + log cost regardless of how we got
    # here. If the LLM blew up mid-stream we still save the partial reply
    # so the chat history isn't out of sync with what the user saw.
    db.add(Conversation(
        session_id=session_id, user_id=user.id, team_id=user.active_team_id,
        role="assistant", content=full_response,
        agent_used=agent_key,
    ))

    if usage_payload:
        await log_api_usage(
            db,
            model=usage_payload["model"],
            prompt_tokens=usage_payload["prompt_tokens"],
            completion_tokens=usage_payload["completion_tokens"],
            user_id=user.id, team_id=user.active_team_id,
            agent_key=agent_key, endpoint="chat-stream",
        )

    await db.flush()
    yield f"data: {json.dumps({'t': 'done'})}\n\n"


__all__ = ["send_message", "stream_message"]
