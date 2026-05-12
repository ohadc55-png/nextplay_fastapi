"""Agent tools — factory-closure pattern (Phase 5 batch 5).

Why factory closures? The fundamental security invariant of NEXTPLAY is
multi-tenancy: every agent action MUST scope to the requesting coach's
`(user_id, team_id)`. If we exposed those as tool parameters, the LLM
could (accidentally or via prompt injection) pass another coach's IDs
and cross-tenant data would leak. Instead, we take `(user_id, team_id)`
when constructing the tool — the handler closes over those values, and
the OpenAI function schema we publish to the model has no fields for
them. The model literally cannot ask for someone else's data.

Mirrors the v1 pattern at `backend/tools.py` `make_team_facts_tool` etc.
This module is the async port; CrewAI's `BaseTool` (sync) integration
lands when CrewAI orchestration ships in a later batch.

Tools currently exposed:
  - query_team_database:   get_player_details / list_recent_games /
                           list_uploads / get_team_profile
  - search_knowledge_base: stub until ChromaDB wraps in batch 6 — emits
                           a deterministic "knowledge base coming online"
                           reply so the chat surface degrades gracefully.

Public API:
  - `Tool`                   immutable dataclass (name, description,
                             parameters JSON schema, handler coroutine,
                             `openai_schema()` method)
  - `make_team_database_tool(db, *, user_id, team_id) -> Tool`
  - `make_knowledge_base_tool(db, *, user_id, team_id) -> Tool`
  - `default_tools_for_agent(agent_key, db, *, user_id, team_id)
        -> list[Tool]`
  - `execute_tool_call(tools, name, arguments) -> dict`
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.players import Player, PlayerGameStat
from src.models.teams import TeamProfile
from src.models.uploads import Upload

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """A tool the agent can call.

    `parameters` is a JSON Schema object describing only the fields the
    LLM is allowed to provide. Tenant fields (user_id, team_id) live in
    the handler's closure and are deliberately absent here.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[dict[str, Any]]]

    def openai_schema(self) -> dict[str, Any]:
        """Format expected by `tools=[...]` on chat.completions.create."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# Helpers shared by the team-database handler
# ---------------------------------------------------------------------------


def _player_to_dict(p: Player) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "number": p.number,
        "position": p.position,
        "height": p.height,
        "weight": p.weight,
        "age": p.age,
        "strengths": p.strengths,
        "weaknesses": p.weaknesses,
        "dominant_hand": p.dominant_hand,
        "notes": p.notes,
    }


def _game_to_dict(g: PlayerGameStat) -> dict[str, Any]:
    return {
        "game_date": g.game_date,
        "opponent": g.opponent,
        "minutes": g.minutes,
        "points": g.points,
        "fgm": g.fgm,
        "fga": g.fga,
        "three_pm": g.three_pm,
        "three_pa": g.three_pa,
        "ftm": g.ftm,
        "fta": g.fta,
        "reb": g.reb,
        "ast": g.ast,
        "stl": g.stl,
        "blk": g.blk,
        "turnovers": g.turnovers,
        "plus_minus": g.plus_minus,
    }


def _upload_to_dict(u: Upload) -> dict[str, Any]:
    return {
        "id": u.id,
        "filename": u.filename,
        "category": u.category,
        "description": u.description,
        "uploaded_at": u.uploaded_at.isoformat() if u.uploaded_at else None,
        "has_extracted_text": bool(u.content_cache),
    }


async def _find_player(
    db: AsyncSession, *, user_id: int, team_id: int | None, query: str
) -> Player | None:
    """Match by exact jersey number first, then case-insensitive name
    substring. Returns the active match scoped to the closure tenant."""
    q = (query or "").strip()
    if not q:
        return None

    base = select(Player).where(Player.user_id == user_id, Player.active.is_(True))
    if team_id is not None:
        base = base.where(Player.team_id == team_id)

    # Try jersey number first (handles "#7" or "7" or "Number 7")
    digits = "".join(ch for ch in q if ch.isdigit())
    if digits:
        try:
            num = int(digits)
            stmt = base.where(Player.number == num).limit(1)
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is not None:
                return row
        except ValueError:
            pass

    # Fall back to ILIKE-style name match. SQLite is case-insensitive on
    # ASCII LIKE by default; Postgres is case-sensitive — use lower() so
    # "doncic" matches "Doncic" on both engines.
    from sqlalchemy import func as sql_func

    stmt = base.where(
        sql_func.lower(Player.name).like(f"%{q.lower()}%")
    ).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def make_team_database_tool(
    db: AsyncSession,
    *,
    user_id: int,
    team_id: int | None,
) -> Tool:
    """Build a tool that lets the agent fetch tenant-scoped team data.

    `user_id` and `team_id` are captured in the closure — the OpenAI
    function schema does NOT expose them. If a malicious prompt tries
    to inject `user_id=42` it'll be silently dropped (TypeError → caught
    by `execute_tool_call`).
    """

    async def handler(
        action: str,
        player_name: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        action = (action or "").strip().lower()

        if action == "get_team_profile":
            if team_id is None:
                return {"error": "no_active_team"}
            profile = (
                await db.execute(select(TeamProfile).where(TeamProfile.id == team_id))
            ).scalar_one_or_none()
            if profile is None:
                return {"error": "team_not_found"}
            return {
                "team_name": profile.team_name,
                "league": profile.league,
                "division": profile.division,
                "play_style": profile.play_style,
                "strengths": profile.strengths,
                "weaknesses": profile.weaknesses,
                "notes": profile.notes,
            }

        if action == "list_roster":
            stmt = (
                select(Player)
                .where(Player.user_id == user_id, Player.active.is_(True))
                .order_by(Player.number.is_(None), Player.number, Player.name)
            )
            if team_id is not None:
                stmt = stmt.where(Player.team_id == team_id)
            rows = (await db.execute(stmt)).scalars().all()
            return {"players": [_player_to_dict(p) for p in rows]}

        if action == "get_player_details":
            if not player_name:
                return {"error": "player_name required"}
            p = await _find_player(
                db, user_id=user_id, team_id=team_id, query=player_name
            )
            if p is None:
                return {"error": "player_not_found", "query": player_name}
            return {"player": _player_to_dict(p)}

        if action == "list_recent_games":
            if not player_name:
                return {"error": "player_name required"}
            p = await _find_player(
                db, user_id=user_id, team_id=team_id, query=player_name
            )
            if p is None:
                return {"error": "player_not_found", "query": player_name}
            stmt = (
                select(PlayerGameStat)
                .where(
                    PlayerGameStat.player_id == p.id,
                    PlayerGameStat.user_id == user_id,
                )
                .order_by(PlayerGameStat.game_date.desc())
                .limit(max(1, min(int(limit or 5), 20)))
            )
            if team_id is not None:
                stmt = stmt.where(PlayerGameStat.team_id == team_id)
            rows = (await db.execute(stmt)).scalars().all()
            return {
                "player": {"id": p.id, "name": p.name, "number": p.number},
                "games": [_game_to_dict(g) for g in rows],
            }

        if action == "list_uploads":
            stmt = select(Upload).where(Upload.user_id == user_id)
            if team_id is not None:
                stmt = stmt.where(Upload.team_id == team_id)
            stmt = stmt.order_by(Upload.uploaded_at.desc().nulls_last()).limit(
                max(1, min(int(limit or 10), 50))
            )
            rows = (await db.execute(stmt)).scalars().all()
            return {"uploads": [_upload_to_dict(u) for u in rows]}

        return {"error": f"unknown action: {action}"}

    return Tool(
        name="query_team_database",
        description=(
            "Look up tenant-scoped team data on demand. Use this when the "
            "coach asks about a specific player's stats, the team profile, "
            "or what files they've uploaded. Always scoped to the active "
            "team — do not pass team identifiers."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "get_team_profile",
                        "list_roster",
                        "get_player_details",
                        "list_recent_games",
                        "list_uploads",
                    ],
                    "description": "Which read action to perform.",
                },
                "player_name": {
                    "type": "string",
                    "description": (
                        "Required for get_player_details / list_recent_games. "
                        "Accepts jersey number ('#7' or '7') or partial name "
                        "('doncic')."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Max rows for list_recent_games / list_uploads.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def make_knowledge_base_tool(
    db: AsyncSession,
    *,
    user_id: int,
    team_id: int | None,
    kb: Any | None = None,
) -> Tool:
    """Tool that searches the basketball coaching knowledge base.

    The KB is **shared across coaches** — drills, plays, scouting
    frameworks. It is NOT a tenant-private store; coach-private
    vectors live in `memories.embedding_json` (separate path).

    `user_id`/`team_id` parameters are kept for symmetry with the
    other factory (`make_team_database_tool`) and to make future
    per-tenant filtering trivial — but they are NOT exposed in the
    OpenAI schema.

    Inject `kb` for tests. Falls back to the process singleton in
    production so the chat handler doesn't have to thread it through.
    """
    from src.crew.knowledge_base import get_kb

    kb_instance = kb if kb is not None else get_kb()

    async def handler(query: str, limit: int = 5) -> dict[str, Any]:
        if not query or not query.strip():
            return {"available": True, "results": [], "message": "empty query"}
        try:
            hits = await kb_instance.search(query.strip(), limit=int(limit or 5))
        except Exception as e:
            logger.warning("[tools.kb] search failed: %s", e)
            return {
                "available": False,
                "message": f"knowledge base unavailable: {e}",
                "results": [],
            }
        if not hits:
            return {
                "available": True,
                "results": [],
                "message": "no matches in knowledge base",
            }
        return {
            "available": True,
            "results": [h.as_dict() for h in hits],
        }

    return Tool(
        name="search_knowledge_base",
        description=(
            "Search the basketball coaching knowledge base for drills, "
            "play diagrams, scouting frameworks, and conditioning programs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Max documents to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def make_research_tool(
    db: AsyncSession,
    *,
    user_id: int,
    team_id: int | None,
) -> Tool:
    """Web research tool — wraps the 8-stage pipeline (`WebResearcher`).

    When the coach asks Scout / Analytics / Tactics / Training about an
    EXTERNAL team, player, or league, the agent calls this tool. The
    pipeline:
      1. Plan — gpt-4o-mini decides what to search for
      2. Search — Serper (10 results / query, parallel)
      3. Triage — gpt-4o-mini ranks snippets by tier + relevance
      4. Fetch — pull top 3 URLs via Jina Reader (4-layer fallback)
    Returns the fetched content as `summary` so the agent can extract
    answers from it. Cache is keyed by (user_id, team_id, ...) — coaches
    never share each other's results.

    Maps to v1's `research_tool` (CrewAI Tool wrapping
    `WebResearcher.research()`). Tenant fields are closure-captured —
    the LLM cannot inject other coaches' IDs."""
    from src.research.web_researcher import ResearchRequest, WebResearcher

    researcher = WebResearcher(db=db)

    async def handler(
        query: str,
        level_hint: str | None = None,
        url_hint: str | None = None,
    ) -> dict[str, Any]:
        if not query or not query.strip():
            return {"error": "query is required"}
        try:
            result = await researcher.research(ResearchRequest(
                user_id=user_id, team_id=team_id,
                query=query.strip(),
                level_hint=(level_hint or "").strip() or None,
                url_hint=(url_hint or "").strip() or None,
            ))
        except Exception as e:
            logger.warning("[tools.research] failed: %s", e)
            return {"error": f"research_failed: {e}"}

        return {
            "summary": result.summary[:8000],   # cap so prompt doesn't explode
            "urls_fetched": list(result.urls_fetched),
            "queries_run": list(result.queries_run),
            "sources": [
                {"url": s.url, "tier": s.tier} for s in result.sources
            ],
            "confidence": result.confidence_overall,
            "cache_hit": result.cache_hit,
            "elapsed_seconds": result.elapsed_seconds,
        }

    return Tool(
        name="research_external_team",
        description=(
            "Research an external team, player, or league via the web "
            "(Serper search + Jina page fetch + tier-1 source ranking). "
            "Use this whenever the coach asks about an opponent we don't "
            "have on the roster, an opposing player's stats, an upcoming "
            "matchup, or a public league standing. ALWAYS call this BEFORE "
            "saying 'I don't have that data' — the tool handles the "
            "search/fetch/extract heavy lifting and returns a content "
            "summary you can build your answer from."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural-language research query, e.g. "
                        "'Maccabi Tel Aviv 2025-26 roster and recent results' "
                        "or 'Hapoel Holon top scorers EuroCup'."
                    ),
                },
                "level_hint": {
                    "type": "string",
                    "description": (
                        "Optional league/level hint to bias the planner "
                        "(e.g. 'Israel Premier League', 'EuroLeague', "
                        "'NCAA D1', 'NBA')."
                    ),
                },
                "url_hint": {
                    "type": "string",
                    "description": (
                        "Optional URL the coach pasted — fast path that "
                        "skips planning + search and fetches just this page."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def make_add_player_tool(
    db: AsyncSession,
    *,
    user_id: int,
    team_id: int | None,
) -> Tool:
    """Tool that lets Brad (GM) create a new player on the active team.

    Used during the `?onboarding=scouting` flow so the coach can build
    their roster purely through chat instead of bouncing to /team-setup.
    Tenant fields (`user_id`, `team_id`) are closure-captured — the LLM
    cannot inject another coach's IDs. Refuses to write when there's no
    active team (the model gets a clear error and can explain it back
    to the coach instead of silently dropping the player)."""

    async def handler(
        name: str,
        number: int | None = None,
        position: str | None = None,
        height: str | None = None,
        weight: str | None = None,
        age: int | None = None,
        strengths: str | None = None,
        weaknesses: str | None = None,
        notes: str | None = None,
        dominant_hand: str | None = None,
    ) -> dict[str, Any]:
        if team_id is None:
            return {"error": "no_active_team",
                    "message": "Cannot add player — no active team selected"}
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            return {"error": "name_required",
                    "message": "Player name is required"}

        # Position normalization — accept the abbreviations the SPA uses
        # (PG/SG/SF/PF/C) plus full names. The team_database tool reads
        # the same column, so keeping a consistent shape avoids
        # downstream "list_roster shows X but get_player_details shows Y".
        valid_positions = {"PG", "SG", "SF", "PF", "C"}
        pos = (position or "").strip().upper() if position else None
        if pos and pos not in valid_positions:
            # Map common variants
            alias = {
                "POINT GUARD": "PG", "SHOOTING GUARD": "SG",
                "SMALL FORWARD": "SF", "POWER FORWARD": "PF",
                "CENTER": "C", "GUARD": "PG", "FORWARD": "SF",
            }
            pos = alias.get(pos)

        try:
            player = Player(
                user_id=user_id, team_id=team_id,
                name=cleaned_name,
                number=int(number) if number is not None else None,
                position=pos,
                height=(height or "").strip() or None,
                weight=(weight or "").strip() or None,
                age=int(age) if age is not None else None,
                strengths=(strengths or "").strip() or None,
                weaknesses=(weaknesses or "").strip() or None,
                notes=(notes or "").strip() or None,
                dominant_hand=(dominant_hand or "").strip() or None,
                active=True,
            )
            db.add(player)
            await db.flush()
            await db.refresh(player)
        except (ValueError, TypeError) as e:
            logger.warning("[tools.add_player] bad input: %s", e)
            return {"error": "bad_input", "message": str(e)}
        except Exception as e:
            logger.exception("[tools.add_player] insert failed")
            return {"error": "insert_failed", "message": str(e)}

        return {
            "success": True,
            "player": {
                "id": player.id,
                "name": player.name,
                "number": player.number,
                "position": player.position,
            },
        }

    return Tool(
        name="add_player",
        description=(
            "Create a new player on the coach's active team. Use this "
            "during onboarding when the coach describes a player who "
            "isn't yet on the roster. Pull whatever fields the coach "
            "mentioned — leave the rest empty; do NOT invent values. "
            "After adding, list_roster will show the new player and "
            "metrics get filled in by a follow-up extraction step."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Full name. Required.",
                },
                "number": {
                    "type": "integer",
                    "description": "Jersey number (0-99 typical).",
                },
                "position": {
                    "type": "string",
                    "enum": ["PG", "SG", "SF", "PF", "C"],
                    "description": "Position abbreviation.",
                },
                "height": {
                    "type": "string",
                    "description": "Free-form (e.g. '1.85m' or '6'1\"').",
                },
                "weight": {
                    "type": "string",
                    "description": "Free-form (e.g. '78kg' or '170 lbs').",
                },
                "age": {"type": "integer"},
                "strengths": {
                    "type": "string",
                    "description": "Comma-separated short list.",
                },
                "weaknesses": {
                    "type": "string",
                    "description": "Comma-separated short list.",
                },
                "notes": {"type": "string"},
                "dominant_hand": {
                    "type": "string",
                    "enum": ["right", "left", "ambidextrous"],
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=handler,
    )


# Map agent_key → tool factories. Mirrors v1 backend/agents.py per-agent
# tool lists:
#   - GM           : team_db + knowledge_base + add_player (onboarding-roster)
#   - Scout        : + research (THIS is what makes "scout opponent X" actually work)
#   - Analytics    : + research (cross-team statistical comparisons)
#   - Tactics      : + research (opponent tendencies, league trends)
#   - Training     : + research (drill libraries, coaching content)
_AGENT_TOOL_MAP: dict[str, tuple[str, ...]] = {
    "gm":        ("team_database", "knowledge_base", "add_player"),
    "scout":     ("team_database", "knowledge_base", "research"),
    "analytics": ("team_database", "knowledge_base", "research"),
    "tactics":   ("team_database", "knowledge_base", "research"),
    "training":  ("team_database", "knowledge_base", "research"),
}


def default_tools_for_agent(
    agent_key: str,
    db: AsyncSession,
    *,
    user_id: int,
    team_id: int | None,
) -> list[Tool]:
    """Return the tool list this agent should be allowed to call."""
    factories = {
        "team_database": make_team_database_tool,
        "knowledge_base": make_knowledge_base_tool,
        "research": make_research_tool,
        "add_player": make_add_player_tool,
    }
    keys = _AGENT_TOOL_MAP.get(agent_key, ())
    return [
        factories[k](db, user_id=user_id, team_id=team_id) for k in keys
    ]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


async def execute_tool_call(
    tools: list[Tool], name: str, arguments: dict[str, Any] | str
) -> dict[str, Any]:
    """Run the named tool with the given args; never raises.

    OpenAI sends `arguments` as a JSON-encoded string on the wire; some
    libraries decode it for us. Accept both. Unknown names, malformed
    JSON, and handler errors all turn into `{"error": "..."}` so the
    chat loop can feed the result back to the model and let it recover."""
    by_name = {t.name: t for t in tools}
    tool = by_name.get(name)
    if tool is None:
        return {"error": f"unknown tool: {name}"}

    if isinstance(arguments, str):
        try:
            args_obj = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as e:
            return {"error": f"bad JSON arguments: {e}"}
    else:
        args_obj = dict(arguments or {})

    # Defense in depth: if a model somehow tries to inject tenant fields,
    # strip them. The handler signatures don't accept them either, but
    # belt-and-suspenders.
    for forbidden in ("user_id", "team_id"):
        args_obj.pop(forbidden, None)

    try:
        return await tool.handler(**args_obj)
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}
    except Exception as e:
        logger.exception("[tools] handler %s failed", name)
        return {"error": str(e)}


__all__ = [
    "Tool",
    "default_tools_for_agent",
    "execute_tool_call",
    "make_knowledge_base_tool",
    "make_team_database_tool",
]
