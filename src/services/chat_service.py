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
import re
from collections.abc import AsyncIterator
from typing import Any

# Character ranges → ISO 639-1 language code. Order matters: a script
# unique to one family wins over a generic match. Used by
# `_detect_message_language` to enforce reply-language parity in the
# system prompt (GPT obeys the persona's "match the coach's language"
# rule unreliably without an in-context hint, especially on the first
# turn after an English opener).
_LANG_SCRIPTS: tuple[tuple[str, str, str], ...] = (
    ("he", "Hebrew",          r"[֐-׿]"),
    ("ar", "Arabic",          r"[؀-ۿ]"),
    ("ru", "Russian",         r"[Ѐ-ӿ]"),
    ("el", "Greek",           r"[Ͱ-Ͽ]"),
    ("ja", "Japanese",        r"[぀-ゟ゠-ヿ]"),
    ("ko", "Korean",          r"[가-힯]"),
    ("zh", "Chinese",         r"[一-鿿]"),
)


def _detect_message_language(text: str) -> tuple[str, str] | None:
    """Cheap, dependency-free language sniff. Returns (iso_code, name) or
    None when only ASCII/Latin is present — in which case we leave the
    persona's default behaviour alone (English-leaning)."""
    if not text:
        return None
    for code, name, pattern in _LANG_SCRIPTS:
        if re.search(pattern, text):
            return code, name
    return None


def _language_directive(user_message: str, history: list[dict]) -> str:
    """Build a strong system-prompt suffix that forces the LLM to reply
    in the coach's language. Looks at the current turn first, then walks
    back through recent coach messages so a single English follow-up
    inside a Hebrew thread doesn't flip the model."""
    sample = user_message or ""
    if not _detect_message_language(sample):
        for h in reversed(history):
            if h.get("role") == "user" and h.get("content"):
                sample = h["content"]
                if _detect_message_language(sample):
                    break
    hit = _detect_message_language(sample)
    if not hit:
        return ""
    _code, name = hit
    return (
        "\n\nLANGUAGE LOCK (highest priority — overrides any conflicting "
        f"persona example):\n- The coach is writing in {name}. Your entire "
        f"reply MUST be in {name}. Do not switch to English mid-reply, do "
        "not translate basketball terms back to English, do not preface in "
        "English. Match the coach's language for every word, including "
        "headers, bullet labels, and follow-up questions."
    )

from openai import APIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import AsyncSessionLocal
from src.crew.agents import build_agent_prompt
from src.crew.llm import get_client, log_api_usage, log_response
from src.crew.routing import route_query
from src.crew.tools import Tool, default_tools_for_agent, execute_tool_call
from src.models.conversations import Conversation
from src.models.players import Player
from src.models.teams import TeamProfile
from src.models.users import User
from src.services.memory_extractor import extract_and_store

logger = logging.getLogger(__name__)


# Cap on history we send to the LLM. v1 uses 12; matching it.
_HISTORY_LIMIT = 12

# Chat model. Mirrors v1 backend/services/chat_service.py:92 + the 5
# specialist agents in backend/crew/manager.py — all use gpt-5.4-mini
# for the actual chat turn (the router/classifier uses the cheaper
# gpt-4o-mini in src/crew/routing.py).
_MODEL = "gpt-5.4-mini"

# Cap on tool-calling round-trips per chat turn. Each iteration costs
# tokens, and a runaway model could in theory call tools forever. v1's
# CrewAI orchestrator caps this at 3-5 in practice; we mirror that.
_TOOL_LOOP_MAX_ITERS = 3


async def _build_team_context(
    db: AsyncSession, *, user_id: int, team_id: int | None
) -> str:
    """Compose the per-request team context string that gets injected into
    every agent's system prompt. Mirrors v1 build_team_context — we list
    the team profile + active roster so the LLM can reference players by
    name/number without inventing.

    Returns an empty string when the user has no active team."""
    if team_id is None:
        return ""

    from sqlalchemy import select

    profile = (await db.execute(
        select(TeamProfile).where(TeamProfile.id == team_id)
    )).scalar_one_or_none()
    players = list((await db.execute(
        select(Player)
        .where(Player.team_id == team_id, Player.active.is_(True))
        .order_by(Player.number.is_(None), Player.number, Player.name)
    )).scalars().all())

    parts: list[str] = []
    if profile:
        parts.append(
            f"Team: {profile.team_name}"
            + (f" — League: {profile.league}" if profile.league else "")
            + (f" — Division: {profile.division}" if profile.division else "")
        )
        if profile.play_style:
            parts.append(f"Play style: {profile.play_style}")
        if profile.strengths:
            parts.append(f"Strengths: {profile.strengths}")
        if profile.weaknesses:
            parts.append(f"Weaknesses: {profile.weaknesses}")

    if players:
        parts.append("Active roster:")
        for p in players:
            label = f"  #{p.number} {p.name}" if p.number is not None else f"  {p.name}"
            if p.position:
                label += f" ({p.position})"
            parts.append(label)

    return "\n".join(parts) if parts else ""


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
    *, system_prompt: str, history: list[dict], user_message: str
) -> list[dict]:
    """Compose the [system, ...history, user] payload."""
    return [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": user_message},
    ]


# ---------------------------------------------------------------------------
# Tool-calling loop helper
# ---------------------------------------------------------------------------

async def _run_with_tools(
    *,
    client,
    db: AsyncSession,
    user_id: int,
    team_id: int | None,
    agent_key: str,
    messages: list[dict],
    tools: list[Tool],
) -> tuple[str, list]:
    """Drive the OpenAI tool-calling loop.

    Returns (final_text, all_responses). `all_responses` lets the caller
    log usage on every round-trip (cost tracking invariant §2.9). The
    loop terminates when:
      - the model emits content with no tool_calls (normal exit)
      - we hit `_TOOL_LOOP_MAX_ITERS` (force a final answer with no tools)
      - any OpenAI call raises (let APIError bubble up to the caller)
    """
    all_responses = []
    convo = list(messages)

    for iteration in range(_TOOL_LOOP_MAX_ITERS):
        # On the last allowed iteration, drop the tools — force the
        # model to write a textual answer instead of looping forever.
        kw: dict = {
            "model": _MODEL,
            "messages": convo,
            "temperature": 0.7,
        }
        if iteration < _TOOL_LOOP_MAX_ITERS - 1:
            kw["tools"] = [t.openai_schema() for t in tools]

        resp = await client.chat.completions.create(**kw)
        all_responses.append(resp)

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            return (msg.content or "").strip(), all_responses

        # Append the assistant's tool-calling turn so the next round-trip
        # has the conversation pointer right.
        convo.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            result = await execute_tool_call(
                tools, tc.function.name, tc.function.arguments,
            )
            convo.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })
            logger.debug(
                "[tools] agent=%s called %s → keys=%s",
                agent_key, tc.function.name, sorted(result.keys()),
            )

    # Loop exhausted without a textual answer; ask one more time tools-off.
    final = await client.chat.completions.create(
        model=_MODEL, messages=convo, temperature=0.7,
    )
    all_responses.append(final)
    return (final.choices[0].message.content or "").strip(), all_responses


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------

async def schedule_memory_extraction(
    *,
    user_id: int,
    team_id: int | None,
    session_id: str,
    agent_key: str,
    user_message: str,
    assistant_response: str,
) -> None:
    """Background-task entrypoint. Uses its own DB session — by the
    time this runs, the request's `get_db` has committed and closed.
    Errors are swallowed: the chat response is already on the wire
    and we don't want a memory hiccup to look like a failure."""
    if not user_message or not assistant_response or agent_key == "error":
        return
    try:
        async with AsyncSessionLocal() as session:
            try:
                await extract_and_store(
                    session,
                    user_id=user_id,
                    team_id=team_id,
                    session_id=session_id,
                    agent_key=agent_key,
                    user_message=user_message,
                    assistant_response=assistant_response,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("[memory] extraction task failed")
    except Exception:
        # Pool exhausted, DB unreachable, etc. — swallow, the user is fine.
        logger.exception("[memory] could not open session for extraction")


async def send_message(
    db: AsyncSession,
    *,
    user: User,
    session_id: str,
    message: str,
    agent: str | None = None,
    background: Any | None = None,
    mode: str = "fast",
    onboarding_mode: str | None = None,
    _bypass_length_check: bool = False,
) -> dict:
    """Send a single user message, return the assistant reply.

    `mode`:
      - "fast"  → direct OpenAI tool-loop (2-5s, 1-2 round trips)
      - "full"  → CrewAI multi-step orchestration (30-60s, costs more)
    Mirrors v1.0-flask's two-mode model. The router picks the agent;
    this picks how hard we run it.

    `onboarding_mode`:
      - "scouting" → injects ONBOARDING_SCOUTING context block when the
        resolved agent is GM (Brad) so Brad walks the coach through
        un-profiled players. Mirrors v1's
        `backend/crew/manager.py:_build_onboarding_scouting_context`.
      - None → normal chat.

    `_bypass_length_check`: internal flag used by `send_chat_with_uploads`
    where the enriched payload (file text + Stage-2 instruction) routinely
    exceeds the UI-level 5000-char limit. Coach-typed input is still
    capped by the Pydantic schema on /api/chat.

    Persists both messages to `conversations` and logs cost. Errors are
    caught and surfaced as a friendly response — the chat must never
    500 on the user."""
    if not message:
        raise ValueError("Message is required")
    if not _bypass_length_check and len(message) > 5000:
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

    team_context = await _build_team_context(
        db, user_id=user.id, team_id=user.active_team_id,
    )
    # Auto-route via the 3-layer router when the SPA didn't pin an agent.
    if not agent:
        agent = await route_query(message, team_ctx=team_context)
    agent_key, system_prompt = build_agent_prompt(agent, team_context)

    # If the coach is in the Brad-led scouting walkthrough, append the
    # onboarding context block to the GM's system prompt. The block
    # tells Brad which players are profiled, who's next, what CSV
    # notes already exist, and how chatty to be. Verbatim port of
    # v1's `_build_onboarding_scouting_context` injection point.
    if onboarding_mode == "scouting" and agent_key == "gm" and user.active_team_id:
        from src.services.onboarding_service import build_onboarding_scouting_context

        ob_ctx = await build_onboarding_scouting_context(
            db, team_id=user.active_team_id,
        )
        if ob_ctx:
            system_prompt = f"{system_prompt}\n\n{ob_ctx}"

    # Force reply-language to match the coach's language. The persona's
    # built-in "respond in the same language" rule isn't reliable enough on
    # its own — a fresh thread that opens in English (Brad's static opener)
    # often pulls the model back to English even after the coach types in
    # Hebrew. This directive is dynamic per-turn and goes LAST in the
    # system prompt so it sits closest to the LLM's attention.
    lang_directive = _language_directive(message, history)
    if lang_directive:
        system_prompt = f"{system_prompt}{lang_directive}"

    response_text = ""

    if mode == "full":
        # CrewAI multi-step orchestration. Lazy import keeps fast-mode
        # latency from paying CrewAI's import cost.
        from src.crew.manager import run_full_chat

        try:
            response_text = await run_full_chat(
                db,
                user_id=user.id, team_id=user.active_team_id,
                agent_key=agent_key,
                user_message=message,
                team_context=team_context,
            )
        except Exception as e:
            logger.exception("[chat] full-mode error: %s", e)
            response_text = "Something went wrong. Please try again."
            agent_key = "error"
    else:
        # Fast mode — direct OpenAI tool-loop.
        # user_id/team_id captured in closures — LLM can't override.
        tools = default_tools_for_agent(
            agent_key, db, user_id=user.id, team_id=user.active_team_id,
        )
        try:
            client = get_client()
            messages_payload = _build_messages(
                system_prompt=system_prompt,
                # Trim the history tail — the user msg is below as the
                # current turn.
                history=history[:-1],
                user_message=message,
            )
            response_text, responses = await _run_with_tools(
                client=client, db=db,
                user_id=user.id, team_id=user.active_team_id,
                agent_key=agent_key,
                messages=messages_payload, tools=tools,
            )
            # Log every round-trip so admin /api/api-costs sees the real
            # cost of a tool-heavy turn.
            for r in responses:
                await log_response(
                    db, r,
                    user_id=user.id, team_id=user.active_team_id,
                    agent_key=agent_key, endpoint="chat",
                )
        except APIError as e:
            logger.warning("[chat] OpenAI API error: %s", e)
            response_text = "I'm having trouble reaching my coaching brain right now. Try again in a moment."
            agent_key = "error"
        except Exception as e:
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

    # Memory extraction runs AFTER the response is sent so it never
    # adds latency to the user-facing turn. We hand off only primitives
    # / strings — no SQLAlchemy session — because the request session
    # is closed by the time `background` fires.
    if background is not None and agent_key != "error":
        background.add_task(
            schedule_memory_extraction,
            user_id=user.id,
            team_id=user.active_team_id,
            session_id=session_id,
            agent_key=agent_key,
            user_message=message,
            assistant_response=response_text,
        )

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
    background: Any | None = None,
    onboarding_mode: str | None = None,
) -> AsyncIterator[str]:
    """Yield SSE-formatted chunks. Format mirrors v1 byte-for-byte:
        data: {"t":"chunk","c":"..."}\n\n
        data: {"t":"tool","name":"...","status":"start|done"}\n\n
        data: {"t":"done"}\n\n
    Errors emit `{"t":"error","message":"..."}` and the stream then
    closes — the frontend treats that as a recoverable failure.

    Streaming + tool-calls work together — each iteration streams text
    chunks AND accumulates tool_call deltas. When the model finishes a
    turn with tool_calls, we execute them, append results, and loop. The
    final iteration drops tools to force a textual answer."""
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

    team_context = await _build_team_context(
        db, user_id=user.id, team_id=user.active_team_id,
    )
    if not agent:
        agent = await route_query(message, team_ctx=team_context)
    agent_key, system_prompt = build_agent_prompt(agent, team_context)
    # Inject the Brad-led player walkthrough context when the coach
    # arrived via `?onboarding=scouting`. Same hook v1 has, just lifted
    # to the streaming path so the home-page CTA works end-to-end.
    if onboarding_mode == "scouting" and agent_key == "gm" and user.active_team_id:
        from src.services.onboarding_service import build_onboarding_scouting_context

        ob_ctx = await build_onboarding_scouting_context(
            db, team_id=user.active_team_id,
        )
        if ob_ctx:
            system_prompt = f"{system_prompt}\n\n{ob_ctx}"

    # Reply-language lock — see send_message for rationale.
    lang_directive = _language_directive(message, history)
    if lang_directive:
        system_prompt = f"{system_prompt}{lang_directive}"

    # Tools — same factory closures as fast-mode `send_message` so the
    # LLM can do real work (e.g. Scout calls research_external_team to
    # fetch web data instead of just pretending it searched).
    tools = default_tools_for_agent(
        agent_key, db, user_id=user.id, team_id=user.active_team_id,
    )
    full_response = ""
    usage_payloads: list[dict] = []

    convo: list[dict] = _build_messages(
        system_prompt=system_prompt,
        history=history[:-1],
        user_message=message,
    )

    try:
        client = get_client()
        emitted_chunks = False

        for iteration in range(_TOOL_LOOP_MAX_ITERS):
            # On the last iteration, drop tools so the model must answer
            # with text. Same defensive cap as fast-mode `_run_with_tools`.
            kw: dict = {
                "model": _MODEL,
                "messages": convo,
                "temperature": 0.7,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if tools and iteration < _TOOL_LOOP_MAX_ITERS - 1:
                kw["tools"] = [t.openai_schema() for t in tools]

            stream = await client.chat.completions.create(**kw)

            # Per-iteration accumulators for tool_call deltas. OpenAI
            # streams tool calls as a sequence of deltas keyed by `index`;
            # we rebuild the full call by concatenating arguments chunks.
            tc_buf: dict[int, dict] = {}
            content_this_turn = ""

            async for event in stream:
                if not event.choices:
                    if getattr(event, "usage", None):
                        usage_payloads.append({
                            "model": getattr(event, "model", _MODEL),
                            "prompt_tokens": event.usage.prompt_tokens or 0,
                            "completion_tokens": event.usage.completion_tokens or 0,
                        })
                    continue

                delta = event.choices[0].delta
                if delta is None:
                    continue

                piece = delta.content or ""
                if piece:
                    content_this_turn += piece
                    full_response += piece
                    emitted_chunks = True
                    yield f"data: {json.dumps({'t': 'chunk', 'c': piece})}\n\n"

                # Accumulate tool_call deltas. Each delta carries an
                # index — re-emerging on the same index appends args.
                tc_deltas = getattr(delta, "tool_calls", None) or []
                for tcd in tc_deltas:
                    idx = tcd.index
                    slot = tc_buf.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tcd.id:
                        slot["id"] = tcd.id
                    fn = getattr(tcd, "function", None)
                    if fn is not None:
                        if fn.name:
                            slot["function"]["name"] = fn.name
                        if fn.arguments:
                            slot["function"]["arguments"] += fn.arguments

            # No tool calls this turn → we have the answer; stream done.
            if not tc_buf:
                break

            # Append the assistant's tool-calling turn so the next
            # round-trip sees the right conversation pointer.
            tool_calls_list = [tc_buf[k] for k in sorted(tc_buf.keys())]
            convo.append({
                "role": "assistant",
                "content": content_this_turn or None,
                "tool_calls": tool_calls_list,
            })

            # Execute each tool call. Surface a `t:tool` status event so
            # the SPA can show "Scout is researching..." instead of dead
            # air during the (sometimes 10-30s) research roundtrip.
            for tc in tool_calls_list:
                fn_name = tc["function"]["name"]
                yield (
                    f"data: {json.dumps({'t': 'tool', 'name': fn_name, 'status': 'start'})}\n\n"
                )
                result = await execute_tool_call(
                    tools, fn_name, tc["function"]["arguments"],
                )
                convo.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, default=str),
                })
                logger.debug(
                    "[chat-stream] agent=%s called %s → keys=%s",
                    agent_key, fn_name,
                    sorted(result.keys()) if isinstance(result, dict) else "<non-dict>",
                )
                yield (
                    f"data: {json.dumps({'t': 'tool', 'name': fn_name, 'status': 'done'})}\n\n"
                )
            # Loop continues — next iteration streams the model's
            # response now that it has the tool results.

        # If the loop exhausted iterations without yielding any chunks
        # (model kept calling tools), emit a degraded fallback so the
        # user isn't left staring at a half-stream.
        if not emitted_chunks and not full_response:
            full_response = (
                "I gathered the data but couldn't compose a final answer in time. "
                "Try rephrasing your question."
            )
            yield f"data: {json.dumps({'t': 'chunk', 'c': full_response})}\n\n"

    except APIError as e:
        logger.warning("[chat-stream] OpenAI API error: %s", e)
        yield f"data: {json.dumps({'t': 'error', 'message': 'OpenAI is unreachable. Try again.'})}\n\n"
        agent_key = "error"
    except Exception as e:
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

    # Log every round-trip — tool-call iterations each have their own
    # usage chunk, and a single chat turn can rack up several. Without
    # this, admin /api/api-costs underreports tool-heavy turns.
    for up in usage_payloads:
        await log_api_usage(
            db,
            model=up["model"],
            prompt_tokens=up["prompt_tokens"],
            completion_tokens=up["completion_tokens"],
            user_id=user.id, team_id=user.active_team_id,
            agent_key=agent_key, endpoint="chat-stream",
        )

    await db.flush()

    # Same background-extraction hookup as send_message — fire after
    # the final SSE event so the chat surface stays snappy.
    if background is not None and agent_key != "error" and full_response:
        background.add_task(
            schedule_memory_extraction,
            user_id=user.id,
            team_id=user.active_team_id,
            session_id=session_id,
            agent_key=agent_key,
            user_message=message,
            assistant_response=full_response,
        )

    yield f"data: {json.dumps({'t': 'done'})}\n\n"


# ---------------------------------------------------------------------------
# Chat-upload — handle 1-3 file uploads attached to a chat turn (Phase 7 b4)
# ---------------------------------------------------------------------------


_GAME_STATS_HINT = (
    "\n\nIf any of the uploaded files contains box-score / per-player stats, "
    "follow the [GAME_STATS_JSON] tail-block extraction protocol in the "
    "Stage 2 instruction above."
)


async def _build_uploaded_file_block(
    *,
    filename: str,
    filepath: str,
    user_message: str,
) -> tuple[str, str | None]:
    """Process ONE uploaded file. Returns (text_chunk, image_type).

    For images we run the Vision Stage 1 extractor and return its
    structured scene description. For data files we run the file
    processor and return the extracted text.

    `image_type` is the IMAGE_TYPE tag from Stage 1 (or None for data
    files) so the caller can pick the right Stage 2 instruction block."""
    from src.services import file_processor as fp_module
    from src.services import vision

    if vision.is_image(filename):
        try:
            description = await vision.describe_basketball_image(
                filepath, user_message,
            )
            image_type = vision._detect_image_type(description)
            block = (
                f"━━━━━━━━━━ FILE: {filename} ━━━━━━━━━━\n"
                f"[IMAGE — pre-extracted visual analysis]\n{description}"
            )
            return block, image_type
        except Exception as e:
            logger.exception("[chat-upload] vision Stage 1 failed for %s", filename)
            return (
                f"━━━━━━━━━━ FILE: {filename} ━━━━━━━━━━\n"
                f"[IMAGE — analysis unavailable: {e}]"
            ), None

    extracted = await fp_module.extract_file_content(filepath, filename)
    if extracted is None:
        # Shouldn't happen — image branch above catches images first.
        extracted = "[Unsupported file type]"
    block = (
        f"━━━━━━━━━━ FILE: {filename} ━━━━━━━━━━\n"
        f"[DATA FILE]\n{extracted}"
    )
    return block, None


def _pick_stage2_instruction(image_types: list[str | None]) -> str:
    """When at least one image was uploaded, pick the dominant Stage 2
    instruction block. Priority order matches v1:
      GAME_SCENE > PLAY_DIAGRAM > SHOT_CHART > STAT_SHEET > OTHER

    For pure-data uploads (no images), returns a generic data-file
    instruction."""
    from src.services.vision import _STAGE2_INSTRUCTIONS

    priority = ["GAME_SCENE", "PLAY_DIAGRAM", "SHOT_CHART", "STAT_SHEET", "OTHER"]
    seen = {t for t in image_types if t}
    for kind in priority:
        if kind in seen:
            return _STAGE2_INSTRUCTIONS[kind]

    return (
        "Analyze the uploaded file content(s) above and answer the "
        "coach's question. Combine insights across the files where "
        "relevant. Respond in the same language the coach used."
        + _GAME_STATS_HINT
    )


async def send_chat_with_uploads(
    db: AsyncSession,
    *,
    user: User,
    session_id: str,
    message: str,
    agent: str | None,
    files: list[tuple[str, str]],   # (filename, abs_filepath)
    background: Any | None = None,
) -> dict:
    """Process 1-3 uploads + run a chat turn against the result.

    Builds the same enriched-message format v1 uses (file blocks +
    Stage 2 instruction + coach's question), then delegates to
    `send_message` so tool-calling, persistence, and cost tracking
    behave identically to a normal chat turn."""
    if not files:
        raise ValueError("At least one file is required")
    if len(files) > 3:
        raise ValueError("Too many files (max 3)")

    coach_msg = message.strip() or f"I uploaded {len(files)} files: " + ", ".join(
        f for f, _ in files
    )

    # Process each file; gather text blocks + image types
    image_types: list[str | None] = []
    blocks: list[str] = [
        f"COACH UPLOADED {len(files)} FILE{'S' if len(files) != 1 else ''}.",
        f"COACH'S MESSAGE: {coach_msg}",
        "",
    ]
    for filename, filepath in files:
        block, image_type = await _build_uploaded_file_block(
            filename=filename, filepath=filepath, user_message=coach_msg,
        )
        blocks.append(block)
        image_types.append(image_type)
        blocks.append("")

    instruction = _pick_stage2_instruction(image_types)
    blocks.append(
        f"Treat all content above as ground truth — analyze across all "
        f"{len(files)} file{'s' if len(files) != 1 else ''} together.\n\n"
        f"{instruction}"
    )
    enriched_message = "\n".join(blocks)

    # Hand off to send_message — it owns the persistence + tool loop +
    # cost logging + memory-extraction-task scheduling. Bypass the 5000-
    # char UI cap because the enriched payload (file text + Stage-2
    # instruction + coach question) routinely exceeds it; the Pydantic
    # schema on /api/chat-upload caps the *coach-typed* part at the form
    # boundary already.
    result = await send_message(
        db, user=user, session_id=session_id,
        message=enriched_message, agent=agent,
        background=background,
        _bypass_length_check=True,
    )
    # Tag the response with the filenames so the SPA can show what was processed.
    result["filenames"] = [f for f, _ in files]
    return result


__all__ = [
    "schedule_memory_extraction",
    "send_chat_with_uploads",
    "send_message",
    "stream_message",
]
