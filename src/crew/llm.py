"""Shared AsyncOpenAI client + cost-tracking wrapper.

Async port of `backend/crew/llm.py` + `backend/api_logger.py`. Every
direct OpenAI call in the codebase MUST go through `log_response` so we
have a complete picture of spend per (user, team, agent, model).
Streaming calls capture usage via `stream_options={"include_usage": True}`
on the request side; the final `usage` chunk lands in the same wrapper.
CrewAI internals expose `crew.usage_metrics` which is logged once after
`crew.kickoff()` completes (Phase 5 batch 2).

Pricing table mirrors v1 exactly so the per-call cost figures the admin
panel reports stay byte-identical between Flask and FastAPI windows.
"""

from __future__ import annotations

import logging
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.analytics import ApiUsageLog

logger = logging.getLogger(__name__)


# OpenAI pricing per 1M tokens — kept in sync with v1 backend/api_logger.py:11.
# When OpenAI updates pricing, update both files.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
}


_client_instance: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Lazy AsyncOpenAI client. Built on first use so the module imports
    cleanly when OPENAI_API_KEY isn't set (tests, dev without an env var).
    Timeout + retries match v1 (30s / 2 retries)."""
    global _client_instance
    if _client_instance is None:
        # Pass a placeholder if the real key isn't set — the OpenAI SDK
        # constructor refuses None outright. The placeholder will fail
        # at request time, which is the correct surface for the error.
        _client_instance = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY or "missing",
            timeout=30,
            max_retries=2,
        )
    return _client_instance


def reset_client() -> None:
    """Test hook — drop the cached client so the next get_client()
    re-reads settings.OPENAI_API_KEY."""
    global _client_instance
    _client_instance = None


def _resolve_pricing(model: str) -> dict[str, float]:
    """Match a model name (possibly a dated variant returned by OpenAI
    like `gpt-4o-2024-08-06` or `gpt-4o-mini-2024-07-18`) to its pricing
    row. Exact hit first, then longest-prefix match against MODEL_PRICING
    keys so the most specific known family wins (`gpt-4o-mini-*` resolves
    to `gpt-4o-mini`, not to `gpt-4o`). Unknown models fall back to
    gpt-4o-mini — the cheapest tier — so the spend log understates rather
    than overstates if a new model slips through."""
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    for key in sorted(MODEL_PRICING, key=len, reverse=True):
        if model.startswith(key):
            return MODEL_PRICING[key]
    return MODEL_PRICING["gpt-4o-mini"]


def calc_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost for a single call. See `_resolve_pricing` for how dated
    OpenAI model variants are mapped back to a known family."""
    pricing = _resolve_pricing(model)
    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 6)


async def log_api_usage(
    db: AsyncSession,
    *,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    user_id: int | None = None,
    team_id: int | None = None,
    agent_key: str | None = None,
    endpoint: str = "chat",
) -> None:
    """Append one row to api_usage_logs. Best-effort: a failure here logs
    a warning and swallows the error so a logging hiccup never breaks
    the chat response itself."""
    total = (prompt_tokens or 0) + (completion_tokens or 0)
    cost = calc_cost(model, prompt_tokens, completion_tokens)
    try:
        db.add(ApiUsageLog(
            user_id=user_id, team_id=team_id, agent_key=agent_key,
            model=model,
            prompt_tokens=prompt_tokens or 0,
            completion_tokens=completion_tokens or 0,
            total_tokens=total,
            cost_usd=cost,
            endpoint=endpoint,
        ))
        await db.flush()
    except Exception as e:
        logger.warning("[llm] Failed to log API usage: %s", e)


async def log_response(
    db: AsyncSession,
    response: Any,
    *,
    user_id: int | None = None,
    team_id: int | None = None,
    agent_key: str | None = None,
    endpoint: str = "chat",
) -> None:
    """Call this on every non-streaming OpenAI response. The
    `response.usage` block is what we care about — model name comes from
    `response.model` (which can differ from the requested model when
    OpenAI routes the call to a versioned variant)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    await log_api_usage(
        db,
        model=getattr(response, "model", "unknown"),
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        user_id=user_id, team_id=team_id,
        agent_key=agent_key, endpoint=endpoint,
    )


__all__ = [
    "MODEL_PRICING",
    "calc_cost",
    "get_client",
    "log_api_usage",
    "log_response",
    "reset_client",
]
