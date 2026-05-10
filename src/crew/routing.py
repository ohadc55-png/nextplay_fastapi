"""3-layer agent router — Phase 5 batch 4.

Async port of `backend/crew/routing.py`. Picks which of the 5 agents
(gm/scout/analytics/tactics/training) handles a coach's message when
the SPA didn't specify one.

The same three layers as v1 — same order, same semantics:

  Layer 1 — DETERMINISTIC SHORTCUTS (microseconds, free)
    • Empty message  → gm
    • URL in message → scout (the coach pasted a link to scout)
    • Explicit research keyword (חפש / scout / research / report / box score)
      → scout if the message names an external team, else stays.

  Layer 2 — OWN-TEAM SEMANTIC CHECK (microseconds, free)
    • Possessives ("our team", "שלנו") + roster name match → gm
      (the coach is asking about HIS team, GM handles roster questions)

  Layer 3 — LLM CLASSIFIER (~200ms, ~$0.0001, ambiguous cases only)
    • A gpt-4o-mini call with strict JSON schema → returns one of the 5
      agent keys. Cached per (message, roster-fingerprint) so a tab
      refresh doesn't re-bill.
    • Any failure here defaults to gm (safe default — coach can ask
      again with explicit agent).

The "needs_tools" (fast vs full mode) decision is a separate thing that
lands when CrewAI orchestration arrives. For now every routed answer
goes through fast mode (direct OpenAI streaming).
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

from src.crew.agents import DEFAULT_AGENT
from src.crew.llm import get_client
from src.crew.prompts import ROUTER_PROMPT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layer 1 — deterministic patterns (verbatim from v1 routing.py)
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Hebrew + English research/scout triggers. When present, send to scout.
_RESEARCH_TRIGGERS = (
    "חפש", "חפש באינטרנט", "תחפש", "מצא", "תמצא",
    "scout", "scouting report", "research", "report on",
    "box score", "boxscore", "stats from", "find info",
    "look up",
)

# Stats / analytics signals.
_STATS_TRIGGERS = (
    "stat", "stats", "metric", "metrics", "average", "ppg", "apg", "rpg",
    "efg", "ortg", "drtg", "possessions", "shooting %", "shot chart",
    "סטטיסטיק", "ממוצע",
)

# Practice / training signals.
_TRAINING_TRIGGERS = (
    "practice plan", "drill", "drills", "training program", "periodization",
    "conditioning", "warm-up", "warmup", "אימון", "תרגיל", "תרגילים",
)

# Tactics signals.
_TACTICS_TRIGGERS = (
    "play", "set play", "offensive set", "defensive scheme",
    "pick and roll", "pick-and-roll", "pnr", "zone defense", "man defense",
    "press break", "transition", "טקטיק", "מערך", "מהלך",
)


# Pull player names out of team_ctx for own-team detection. Same regex
# v1 uses to walk the "Name #N" or "#N Name" patterns the team-context
# builder produces.
_PLAYER_NAME_RE = re.compile(
    r"(?:^|\n|\s)([A-Za-z֐-׿][A-Za-z֐-׿\s'\-\.]{1,40}?)"
    r"\s*#\s*\d{1,3}",
    re.MULTILINE,
)

_OWN_TEAM_MARKERS = (
    # Hebrew
    "שלנו", "שלי", "אצלנו", "אצלי",
    "הקבוצה שלנו", "ההרכב שלנו",
    "השחקנים שלנו", "אנחנו",
    # English
    "our team", "our roster", "our players", "our squad", "our lineup",
    "my team", "my roster", "my players", "my squad", "my lineup",
)


def _extract_roster_names(team_ctx: str) -> set[str]:
    """Best-effort player-name extraction from the injected team context."""
    if not team_ctx:
        return set()
    names: set[str] = set()
    for match in _PLAYER_NAME_RE.finditer(team_ctx):
        name = match.group(1).strip().lower()
        name = re.sub(r"^(name|player|שם)\s*[:|-]\s*", "", name, flags=re.IGNORECASE)
        if 2 <= len(name) <= 40:
            names.add(name)
    return names


def _mentions_own_team(message: str, team_ctx: str) -> bool:
    msg = message.lower()
    if any(m in msg for m in _OWN_TEAM_MARKERS):
        return True
    for name in _extract_roster_names(team_ctx):
        if name and name in msg:
            return True
    return False


def _deterministic_pick(message: str, team_ctx: str) -> str | None:
    """Layer 1 + Layer 2 — return an agent key if we can decide cheaply,
    None to fall through to the LLM classifier."""
    if not message or not message.strip():
        return DEFAULT_AGENT

    msg = message.lower()

    # URL → opponent / external content → scout reads it
    if _URL_RE.search(message):
        return "scout"

    # Explicit research/scout vocabulary → scout
    if any(t in msg for t in _RESEARCH_TRIGGERS):
        return "scout"

    # Stats vocabulary → analytics
    if any(t in msg for t in _STATS_TRIGGERS):
        return "analytics"

    # Practice / drill vocabulary → training
    if any(t in msg for t in _TRAINING_TRIGGERS):
        return "training"

    # Tactics vocabulary → tactics
    if any(t in msg for t in _TACTICS_TRIGGERS):
        return "tactics"

    # Layer 2: own-team possessive or roster name → GM
    if _mentions_own_team(message, team_ctx):
        return "gm"

    return None  # fall through to LLM classifier


# ---------------------------------------------------------------------------
# Layer 3 — LLM classifier with structured outputs
# ---------------------------------------------------------------------------

_ROUTER_AGENTS = ("gm", "scout", "analytics", "tactics", "training")

_ROUTING_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "routing_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "One short sentence explaining why this domain fits.",
                },
                "agent": {"type": "string", "enum": list(_ROUTER_AGENTS)},
            },
            "required": ["reasoning", "agent"],
            "additionalProperties": False,
        },
    },
}


@lru_cache(maxsize=512)
def _cache_key(msg_norm: str, roster_fp: str) -> tuple[str, str]:
    """Identity passthrough so the cache decorator dedupes."""
    return msg_norm, roster_fp


async def _llm_classify(message: str, team_ctx: str) -> str:
    """Cached gpt-4o-mini classifier. Returns an agent key or 'gm' on any
    failure (safe default — coach can re-prompt with an explicit agent)."""
    msg_norm = (message or "").strip().lower()[:600]
    roster_fp = (team_ctx or "")[:600].strip()

    # Cache hit fast path
    cached = getattr(_llm_classify, "_results", {}).get((msg_norm, roster_fp))
    if cached:
        return cached

    try:
        client = get_client()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": ROUTER_PROMPT},
                {"role": "user", "content": (
                    f"Coach's roster context: {roster_fp or '(none)'}\n"
                    f"Question: {message}"
                )},
            ],
            temperature=0,
            max_completion_tokens=120,
            response_format=_ROUTING_SCHEMA,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        agent = data.get("agent", "gm")
        if agent not in _ROUTER_AGENTS:
            logger.warning("[router] unknown agent '%s', defaulting to gm", agent)
            agent = "gm"
        logger.info(
            "[router] agent=%s reason=%s",
            agent, (data.get("reasoning") or "")[:120],
        )
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("[router] malformed JSON, defaulting to gm: %s", e)
        agent = "gm"
    except Exception as e:
        logger.warning("[router] LLM call failed, defaulting to gm: %s", e)
        agent = "gm"

    # Stash in a per-process cache. We don't use lru_cache directly because
    # the function is async — but we get the same dedup effect by hand.
    if not hasattr(_llm_classify, "_results"):
        _llm_classify._results = {}
    _llm_classify._results[(msg_norm, roster_fp)] = agent
    return agent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def route_query(message: str, *, team_ctx: str = "") -> str:
    """Pick the best agent for this message. Cheap layers first, LLM
    only on truly ambiguous cases. Always returns a valid agent key."""
    pick = _deterministic_pick(message, team_ctx)
    if pick is not None:
        logger.info("[router] deterministic → %s", pick)
        return pick
    return await _llm_classify(message, team_ctx)


def _reset_cache_for_tests() -> None:
    """Drop the per-process classifier cache. Called in tests so they
    don't share state."""
    if hasattr(_llm_classify, "_results"):
        _llm_classify._results.clear()


__all__ = ["_reset_cache_for_tests", "route_query"]
