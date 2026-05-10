"""CrewAI multi-agent orchestrator — async port of `backend/crew/manager.py`.

Phase 5 batch 10: full-mode chat. The 5 specialist personas (Brad / Hunter
/ Nexus / Vance / Williams) execute through CrewAI when the question
needs sustained reasoning + tool use, instead of the single-shot OpenAI
call that fast mode uses.

Key invariants (per master prompt §5 Phase 5):
  - `crew.kickoff()` is sync-only. We wrap it in `asyncio.to_thread` so
    the FastAPI event loop keeps spinning while CrewAI iterates through
    its agent steps (which can take 30-60 seconds in full mode).
  - `crew.usage_metrics` is read AFTER kickoff to capture token cost.
    CrewAI's internals call litellm directly — bypassing our `client`
    wrapper — so this is the only path that bills full-mode chats.
  - Per-agent tools are factory-closured by `(user_id, team_id)` so the
    LLM cannot inject other coaches' IDs (master prompt §2.1). The
    sync→async bridge in `_AsyncToolBridge` re-uses the closure-keyed
    tools from `src/crew/tools.py` — no parallel sync tool layer.

Lazy import: CrewAI pulls in litellm + opentelemetry + a dozen sub-deps.
Importing it at module-load time slows down every test that doesn't
need it. We import inside `run_full_chat` instead.

What's deferred:
  - Multi-agent crews (e.g., Brad delegates a scout job to Hunter). Today
    each chat turn runs ONE specialist; the routing layer (batch 4)
    picks which one.
  - CrewAI's memory feature — we use our own memory_extractor (batch 7).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.crew.agents import build_agent_prompt
from src.crew.llm import log_api_usage
from src.crew.tools import Tool as AsyncTool

logger = logging.getLogger(__name__)


_FULL_MODE_MODEL = "gpt-5.4-mini"  # matches v1 backend/crew/manager.py:307+


# ---------------------------------------------------------------------------
# Sync→async tool bridge
# ---------------------------------------------------------------------------


def _build_sync_bridge(async_tool: AsyncTool):
    """Wrap an `AsyncTool` (from `src/crew/tools.py`) as a CrewAI sync tool.

    CrewAI runs inside `asyncio.to_thread` — i.e. on a worker thread,
    not the event loop. Calling `asyncio.run(...)` from there safely
    creates a fresh loop for the bridge call.

    The async tool's `(user_id, team_id)` are closure-captured already;
    this bridge just shuttles arguments + return value across the
    sync/async boundary. No tenant fields ever land in CrewAI's
    visible parameter schema."""
    from crewai.tools import BaseTool

    handler = async_tool.handler

    class _Bridge(BaseTool):
        name: str = async_tool.name
        description: str = async_tool.description

        def _run(self, **kwargs: Any) -> str:
            # Strip tenant fields if the LLM tried to inject them
            # (defense in depth — the schema also blocks them)
            kwargs.pop("user_id", None)
            kwargs.pop("team_id", None)
            try:
                result = asyncio.run(handler(**kwargs))
            except Exception as e:
                logger.warning(
                    "[crew.bridge] %s handler failed: %s",
                    async_tool.name, e,
                )
                return f"tool error: {e}"
            # CrewAI tools return strings — serialize structured results.
            if isinstance(result, str):
                return result
            import json

            return json.dumps(result, ensure_ascii=False, default=str)

    bridge = _Bridge()
    return bridge


# ---------------------------------------------------------------------------
# Public API — async full-mode chat
# ---------------------------------------------------------------------------


async def run_full_chat(
    db: AsyncSession,
    *,
    user_id: int,
    team_id: int | None,
    agent_key: str,
    user_message: str,
    team_context: str = "",
    extra_context: str = "",
) -> str:
    """Run one CrewAI turn for the given agent. Returns the result text.

    `extra_context` is anything the caller wants prepended to the task
    description — KB hits, file content extracted by the file_processor,
    research findings. Keeping it as a single string here means the
    composition logic stays in the chat handler where the inputs live.

    Raises only when an upstream import fails; CrewAI's internal errors
    are caught and surfaced as a friendly fallback string so the chat
    handler can persist + return it without 500ing.
    """
    # Lazy imports — keep CrewAI off the hot import path.
    try:
        from crewai import Agent, Crew, Process, Task
    except Exception as e:
        logger.exception("[crew] CrewAI import failed")
        return (
            "Full-mode coaching is temporarily unavailable. Try again, or "
            f"rephrase your question for a quick answer. ({e})"
        )

    # Build the persona prompt + tool kit. Tenancy stays closure-only —
    # the bridge below preserves that.
    resolved_agent_key, system_prompt = build_agent_prompt(agent_key, team_context)
    from src.crew.tools import default_tools_for_agent

    async_tools = default_tools_for_agent(
        resolved_agent_key, db, user_id=user_id, team_id=team_id,
    )
    crewai_tools = [_build_sync_bridge(t) for t in async_tools]

    agent = Agent(
        role=resolved_agent_key,
        goal=f"Answer the coach's question precisely and apply the {resolved_agent_key} persona.",
        backstory=system_prompt,
        tools=crewai_tools,
        verbose=False,
        allow_delegation=False,
        # CrewAI defaults to gpt-4 if we don't pin; mirror the v1 default
        # so cost behavior is predictable. The actual model resolution
        # happens via litellm + the OPENAI_API_KEY env var.
        llm=_FULL_MODE_MODEL,
    )

    description_parts: list[str] = []
    if extra_context.strip():
        description_parts.append(extra_context.strip())
    description_parts.append(f"COACH'S REQUEST:\n{user_message}")
    description_parts.append(
        "Answer ONLY what the coach asked. Be precise and focused."
    )
    task = Task(
        description="\n\n".join(description_parts),
        expected_output=(
            "A focused, precise answer in clean plain text. No markdown "
            "formatting (no #, *, **, ```). Use numbered lists or dashes "
            "for structure. Respond in the same language the coach uses."
        ),
        agent=agent,
    )

    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )

    # crew.kickoff() blocks for tens of seconds in full mode. asyncio.to_thread
    # offloads it to the default thread pool so the event loop stays responsive
    # to other coaches.
    try:
        result = await asyncio.to_thread(crew.kickoff)
    except Exception:
        # Known CrewAI failure mode: tool_calls history corruption. The v1
        # path falls back to fast mode here; we surface a friendly note
        # and let the chat handler decide whether to retry. Keeping this
        # async-pure (no fast-mode reentry) avoids a circular dep on
        # chat_service from inside the manager.
        logger.exception("[crew] kickoff failed for %s", resolved_agent_key)
        return (
            "I had trouble running the full multi-step analysis. "
            "Try rephrasing the question or breaking it into smaller parts."
        )

    # Capture cost from CrewAI's aggregated usage. Without this, every
    # full-mode chat would be unbilled in api_usage_logs.
    try:
        usage = getattr(crew, "usage_metrics", None)
        if usage is not None:
            pt = int(getattr(usage, "prompt_tokens", 0) or 0)
            ct = int(getattr(usage, "completion_tokens", 0) or 0)
            if pt or ct:
                await log_api_usage(
                    db,
                    model=_FULL_MODE_MODEL,
                    prompt_tokens=pt, completion_tokens=ct,
                    user_id=user_id, team_id=team_id,
                    agent_key=resolved_agent_key,
                    endpoint="crew-full",
                )
    except Exception as e:
        logger.debug("[crew] usage_metrics logging skipped: %s", e)

    return str(result)


__all__ = ["run_full_chat"]
