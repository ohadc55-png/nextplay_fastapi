"""Memory extractor — pulls durable coaching memories from chat turns.

Why this exists: LLMs are stateless. Without a memory layer, the agents
forget that coach Sara prefers high-intensity practices, that her starting
center has a bad knee, and that she's running a 1-3-1 zone this season.
Every chat would start from zero. The memory extractor watches each
turn, asks gpt-4o-mini "what's worth remembering here?", and stores
durable items in the `memories` table for next-session recall.

Smart team scoping (mirrors v1.0-flask):
  - style / preference / philosophy → team_id = NULL  (coach-personal,
    visible from any team this coach owns).
  - insight / decision / pattern / fact → team_id = <active team>
    (team-specific; another team gets no benefit).

This is the most product-shaping invariant in the AI layer. Per the
master prompt §2.5: "If you change this, you break the product."

Background-task discipline:
  - Runs AFTER the chat response is sent (FastAPI BackgroundTasks).
  - Has its own DB session — cannot reuse the request session because
    `get_db` has already committed + closed it by the time we run.
  - NEVER raises into the caller — log + swallow so a bad turn doesn't
    blow up subsequent extractions.

Cost discipline: 1500 max_tokens cap + JSON-parse retry (one extra
attempt with a stricter "JSON ONLY" reminder), then give up. Every
OpenAI call goes through `log_response` so admin /api/api-costs
catches the cost.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import APIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.crew.llm import get_client, log_response
from src.models.memory import Memory

logger = logging.getLogger(__name__)


_MODEL = "gpt-5.4-mini"  # matches v1 backend/memory_extractor.py:120,274
_MAX_TOKENS = 1500

# Categories that apply across ALL the coach's teams. Stored with
# team_id=NULL.
_COACH_PERSONAL_CATEGORIES = {"style", "preference", "philosophy"}

# Categories that bind to a specific team. Stored with team_id=<active>.
_TEAM_SPECIFIC_CATEGORIES = {"insight", "decision", "pattern", "fact"}

_VALID_CATEGORIES = _COACH_PERSONAL_CATEGORIES | _TEAM_SPECIFIC_CATEGORIES


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """You read a chat turn between a basketball coach and an AI assistant
and extract durable coaching memories worth remembering for future
sessions.

CATEGORIES — use exactly these keys:
  style       — how this coach communicates / runs practice / coaches.
                Example: "prefers high-intensity drills over walkthroughs"
  preference  — stable likes/dislikes about basketball or coaching.
                Example: "dislikes 1-on-1 drills, never assigns them"
  philosophy  — coaching principles / values.
                Example: "defense always before offense"
  insight     — what was learned about a specific player or team.
                Example: "Doncic struggles when switched onto bigs"
  decision    — a concrete choice the coach made or is considering.
                Example: "starting Smith over Jones for next 5 games"
  pattern     — recurring observation about the team or opponents.
                Example: "team's offense stalls in the 3rd quarter"
  fact        — concrete data point worth remembering.
                Example: "team plays in EuroLeague Division B, 4-3 record"

RULES:
1. Only extract memories that will still be useful 30 days from now.
   Skip greetings, fleeting opinions, restated questions.
2. Each memory must be a single complete sentence (15-50 words).
3. Importance is 1 (trivial — almost worth skipping) to 10 (critical —
   must remember forever). Most memories are 4-7.
4. If nothing is worth remembering, return an empty array.

Respond with VALID JSON ONLY in exactly this shape:
{"memories": [{"category": "<one of: style|preference|philosophy|insight|decision|pattern|fact>", "content": "<sentence>", "importance": <1-10>}, ...]}

No prose, no markdown, no fenced blocks — just the JSON object."""


def _build_extraction_messages(
    *, user_message: str, assistant_response: str, agent_key: str | None
) -> list[dict]:
    agent_hint = f" (agent: {agent_key})" if agent_key else ""
    return [
        {"role": "system", "content": _EXTRACTION_PROMPT},
        {
            "role": "user",
            "content": (
                f"COACH SAID:\n{user_message}\n\n"
                f"ASSISTANT REPLIED{agent_hint}:\n{assistant_response}\n\n"
                "Extract memories now."
            ),
        },
    ]


# ---------------------------------------------------------------------------
# JSON parsing with retry
# ---------------------------------------------------------------------------

def _try_parse(text: str) -> dict | None:
    """Strip code fences (in case the model ignores instructions),
    parse JSON, return None on failure."""
    if not text:
        return None
    s = text.strip()
    # Some models still wrap in ```json ... ``` despite "JSON only" rule
    if s.startswith("```"):
        s = s.lstrip("`").lstrip("json").strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Validation + scoping
# ---------------------------------------------------------------------------

def _scope_team_id(category: str, active_team_id: int | None) -> int | None:
    """Return the team_id this memory should bind to.
    - coach-personal categories  → NULL  (cross-team for this coach)
    - team-specific categories   → active_team_id  (or NULL if no team)
    The latter NULL fallback matches v1.0-flask's behavior when a coach
    chats before selecting a team.
    """
    if category in _COACH_PERSONAL_CATEGORIES:
        return None
    return active_team_id


def _validate_memory(item: Any) -> dict | None:
    """Coerce one extracted item into a sane row, or drop it.
    Quietly clamping importance and dropping garbage keeps the
    extractor robust to small model deviations."""
    if not isinstance(item, dict):
        return None
    cat = (item.get("category") or "").strip().lower()
    content = (item.get("content") or "").strip()
    if cat not in _VALID_CATEGORIES:
        return None
    if not content or len(content) < 4:
        return None
    if len(content) > 1000:
        content = content[:1000]
    importance = item.get("importance")
    try:
        importance = int(importance)
    except (TypeError, ValueError):
        importance = 5
    importance = max(1, min(10, importance))
    return {"category": cat, "content": content, "importance": importance}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def extract_and_store(
    db: AsyncSession,
    *,
    user_id: int,
    team_id: int | None,
    session_id: str,
    agent_key: str | None,
    user_message: str,
    assistant_response: str,
) -> list[int]:
    """Extract memories from one chat turn and persist them.

    Returns the list of newly-created memory IDs (mostly useful for
    tests; production callers can ignore the return value).

    NEVER raises — every error path logs and returns []. The chat
    response has already been sent by the time this runs; we don't want
    to surface failures to the user.
    """
    if not user_message or not assistant_response:
        return []
    if len(user_message.strip()) < 4 or len(assistant_response.strip()) < 4:
        # Too short to extract anything meaningful (e.g. "ok" / "yes")
        return []

    client = get_client()
    base_messages = _build_extraction_messages(
        user_message=user_message,
        assistant_response=assistant_response,
        agent_key=agent_key,
    )

    parsed: dict | None = None
    try:
        resp = await client.chat.completions.create(
            model=_MODEL,
            messages=base_messages,
            temperature=0.2,  # extraction wants determinism, not creativity
            max_tokens=_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        await log_response(
            db, resp,
            user_id=user_id, team_id=team_id,
            agent_key=agent_key, endpoint="memory-extract",
        )
        text = (resp.choices[0].message.content or "")
        parsed = _try_parse(text)
        if parsed is None:
            # Retry once with an explicit "JSON only" reminder. Models
            # sometimes wrap output in prose on the first try.
            retry_messages = [
                *base_messages,
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    "Your previous response was not valid JSON. Reply "
                    "with ONLY the JSON object, no prose."
                )},
            ]
            resp2 = await client.chat.completions.create(
                model=_MODEL,
                messages=retry_messages,
                temperature=0.0,
                max_tokens=_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
            await log_response(
                db, resp2,
                user_id=user_id, team_id=team_id,
                agent_key=agent_key, endpoint="memory-extract-retry",
            )
            parsed = _try_parse(resp2.choices[0].message.content or "")
    except APIError as e:
        logger.warning("[memory] OpenAI error during extraction: %s", e)
        return []
    except Exception as e:
        logger.exception("[memory] unexpected error during extraction: %s", e)
        return []

    if not parsed:
        logger.info("[memory] could not parse extraction JSON")
        return []

    items = parsed.get("memories") or []
    if not isinstance(items, list):
        logger.info("[memory] extraction had no 'memories' list")
        return []

    created_ids: list[int] = []
    for raw in items:
        clean = _validate_memory(raw)
        if clean is None:
            continue
        scoped_team_id = _scope_team_id(clean["category"], team_id)
        row = Memory(
            user_id=user_id,
            team_id=scoped_team_id,
            category=clean["category"],
            content=clean["content"],
            source_session_id=session_id,
            agent_key=agent_key,
            importance=clean["importance"],
            active=True,
        )
        db.add(row)
        await db.flush()
        created_ids.append(row.id)

    return created_ids


__all__ = [
    "_COACH_PERSONAL_CATEGORIES",  # exported for tests
    "_TEAM_SPECIFIC_CATEGORIES",
    "extract_and_store",
]
