"""Coach Notebook — entries CRUD + attendance + game stats.

Async port of `backend/notebook/routes.py` + `service.py`. 17 endpoints
total. Multi-tenancy gate: every query scopes by both `user_id` AND
`active_team_id`; if the user has no active team, list endpoints return
empty and writes return 400.

Notes:
  - `format-for-save` calls gpt-4.1-mini to extract structured fields
    per entry_type (practice_plan, tactical_plan, game_summary, etc.).
    Verbatim port of v1's prompt + schemas; on LLM failure returns a
    raw passthrough so the save flow never crashes.
  - Player game-stats attached to a notebook entry use `INSERT OR
    IGNORE` on (player_id, game_date, opponent); we don't double-write
    when the same Game Summary is reprocessed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.database import get_db
from src.models.notebook import NotebookAttendance, NotebookEntry, NotebookEntryPlayer
from src.models.players import Player, PlayerGameStat
from src.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notebook", tags=["notebook"])

# Box-score columns — same order as v1 STAT_COLS so the Python game-stats
# aggregations and per-player upserts stay byte-identical.
_STAT_COLS = [
    "minutes", "points", "fgm", "fga", "three_pm", "three_pa",
    "ftm", "fta", "oreb", "dreb", "reb", "ast", "stl", "blk",
    "turnovers", "pf", "plus_minus",
]


def _require_team(user: User) -> int:
    if user.active_team_id is None:
        raise HTTPException(status_code=400, detail="no active team")
    return user.active_team_id


def _serialize_entry(
    e: NotebookEntry,
    *,
    attendance: list[dict] | None = None,
    player_ids: list[int] | None = None,
) -> dict:
    return {
        "id": e.id,
        "user_id": e.user_id,
        "team_id": e.team_id,
        "entry_type": e.entry_type,
        "title": e.title,
        "entry_date": e.entry_date,
        "content": e.content_json or {},
        "player_id": e.player_id,
        "source": e.source,
        "tags": e.tags_json or [],
        "created_at": e.created_at,
        "updated_at": e.updated_at,
        "attendance": attendance or [],
        "player_ids": player_ids or [],
    }


# ---------------------------------------------------------------------------
# List entries
# ---------------------------------------------------------------------------

@router.get("")
async def list_entries(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    type: str | None = None,
    player_id: int | None = None,
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> dict:
    if user.active_team_id is None:
        return {"success": True, "data": [], "total": 0}

    stmt = select(NotebookEntry).where(
        NotebookEntry.user_id == user.id,
        NotebookEntry.team_id == user.active_team_id,
    )
    if type:
        stmt = stmt.where(NotebookEntry.entry_type == type)
    if player_id:
        # Match either legacy single column OR new M-M join table — same
        # OR semantics as v1 service.list_entries.
        join_subq = select(NotebookEntryPlayer.entry_id).where(
            NotebookEntryPlayer.player_id == player_id
        )
        stmt = stmt.where(
            (NotebookEntry.player_id == player_id) |
            NotebookEntry.id.in_(join_subq)
        )
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            NotebookEntry.title.like(like) |
            NotebookEntry.content_json.cast(text("TEXT")).like(like)
        )

    total = int(
        (await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar() or 0
    )

    stmt = (
        stmt.order_by(NotebookEntry.entry_date.desc(), NotebookEntry.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return {
        "success": True,
        "data": [_serialize_entry(r) for r in rows],
        "total": total,
    }


# ---------------------------------------------------------------------------
# Create entry
# ---------------------------------------------------------------------------

class _EntryCreateBody(BaseModel):
    entry_type: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    entry_date: str | None = None
    content: Any | None = None
    player_id: int | None = None
    source: str | None = "manual"
    tags: list[str] | None = None
    player_ids: list[int] | None = None
    attendance: list[dict] | None = None


@router.post("", status_code=201)
async def create_entry(
    body: _EntryCreateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    team_id = _require_team(user)

    now = datetime.utcnow().isoformat()
    entry_date = body.entry_date or date.today().isoformat()

    content = body.content if isinstance(body.content, dict) else (
        {"content": body.content} if body.content else {}
    )

    entry = NotebookEntry(
        user_id=user.id,
        team_id=team_id,
        entry_type=body.entry_type,
        title=body.title.strip(),
        entry_date=entry_date,
        content_json=content,
        player_id=int(body.player_id) if body.player_id else None,
        source=body.source or "manual",
        tags_json=body.tags or [],
        created_at=now,
        updated_at=now,
    )
    db.add(entry)
    await db.flush()

    # Attendance rows (if provided + entry is attendance type)
    if body.entry_type == "attendance" and body.attendance:
        for rec in body.attendance:
            db.add(NotebookAttendance(
                entry_id=entry.id,
                player_id=int(rec["player_id"]),
                status=rec.get("status", "present"),
                note=rec.get("note") or "",
            ))

    # Many-to-many player tags (legacy player_id is added too).
    combined: list[int] = list(body.player_ids or [])
    if body.player_id:
        try:
            combined.append(int(body.player_id))
        except (TypeError, ValueError):
            pass
    seen: set[int] = set()
    for pid in combined:
        if pid in seen:
            continue
        seen.add(pid)
        db.add(NotebookEntryPlayer(entry_id=entry.id, player_id=pid))

    await db.flush()
    return {
        "success": True,
        "data": _serialize_entry(
            entry,
            player_ids=sorted(seen),
        ),
    }


# ---------------------------------------------------------------------------
# Format-for-save (LLM stub — Phase 5)
# ---------------------------------------------------------------------------

# Schemas mirror the fields used by the manual entry forms in notebook.html so
# AI-formatted entries land with the same shape a coach would produce by hand.
# Verbatim port of v1's backend/notebook/routes.py:73 — when adding a new
# entry_type to the manual forms, add its schema here too or the LLM will
# silently fall back to a raw passthrough for that type.
_FORMAT_SCHEMAS: dict[str, str] = {
    "practice_plan": (
        '{"topic":"","duration":"","warmup":"","main_part":"","cooldown":"",'
        '"notes":"","post_summary":"","what_worked":"","what_didnt":"",'
        '"next_focus":""}'
    ),
    "game_summary": (
        '{"opponent":"","score_us":0,"score_them":0,"quarter_scores":[],'
        '"what_worked":"","what_didnt":"","standout_players":"",'
        '"next_practice_focus":"","notes":""}'
    ),
    "period_plan": (
        '{"period_type":"annual|semi_annual|monthly|weekly",'
        '"period_start":"","period_end":"","focus":"",'
        '"season_goals":"","dev_priorities":"","milestones":"","periodization":"",'
        '"monthly_goals":"","weekly_breakdown":"","key_games":"","focus_areas":"",'
        '"daily_breakdown":{"sunday":"","monday":"","tuesday":"","wednesday":"","thursday":"","friday":""},'
        '"game_prep":"","notes":""}'
    ),
    "tactical_plan": (
        '{"scenario":"","offense":"","defense":"","transition":"",'
        '"set_plays":"","vs_press":"","vs_zone":"","key_principles":"",'
        '"matchup_notes":"","notes":""}'
    ),
    "player_note": '{"content":""}',
    "free_document": '{"content":""}',
}

# Per-type guidance to help the model fill the right sub-fields. Empty string
# = no special guidance (the schema alone is enough).
_FORMAT_GUIDANCE: dict[str, str] = {
    "period_plan": (
        "Detect period_type from the text — 'annual'/'שנתי' for full-year plans, "
        "'semi_annual'/'חצי שנתי' for half-year, 'monthly'/'חודשי' for one-month, "
        "'weekly'/'שבועי' for one-week. Then ONLY fill the sub-fields that match: "
        "annual/semi_annual → season_goals, dev_priorities, milestones, periodization. "
        "monthly → monthly_goals, weekly_breakdown, key_games, focus_areas. "
        "weekly → daily_breakdown (object with sunday..friday text). "
        "Always fill: focus, game_prep, notes when mentioned. Leave irrelevant "
        "sub-fields as empty string (or empty object for daily_breakdown)."
    ),
    "tactical_plan": (
        "Extract tactical principles from the text. scenario = the situation "
        "(e.g. 'half-court vs man defense', 'late-game'). offense/defense = "
        "core principles. transition = fast break / transition defense. "
        "set_plays = named plays or specific actions. vs_press/vs_zone = "
        "answers to those defensive looks. key_principles = the core ideas. "
        "matchup_notes = specific player matchups or assignments."
    ),
    "game_summary": (
        "score_us / score_them must be integers if mentioned. quarter_scores "
        "is an array of {us, them} objects per quarter — leave [] if not given."
    ),
}


@router.post("/format-for-save")
async def format_for_save(
    body: dict = Body(...),
    user: User = Depends(get_current_user),
    # NOTE: deliberately NO `db = Depends(get_db)` here. The gpt-4.1-mini
    # call below blocks for 3-10s and would pin a SQLite connection for the
    # whole window — long enough for the subsequent /api/notebook POST
    # (saving the formatted entry) to time out on a busy_timeout=5s lock
    # contention. We open a short-lived session for cost-logging only,
    # AFTER the LLM returns and we're done with the network call.
) -> dict:
    """Use gpt-4.1-mini to reformat free-form chat content into the structured
    JSON the notebook UI expects per entry_type. Verbatim port of v1's
    notebook/routes.py:129 logic — same schemas, same system prompt, same
    fallback contract (always returns {formatted: {...}, warning?: ...},
    never raises to the client, so the SPA's save flow can't crash here).
    """
    import json as _json

    from src.core.database import AsyncSessionLocal
    from src.crew.llm import get_client, log_response

    raw_content = (body.get("content") or "").strip()
    entry_type = body.get("entry_type") or "free_document"

    if not raw_content:
        raise HTTPException(status_code=400, detail="No content provided")

    # Free-form types — no structuring to do, return as-is.
    if entry_type in ("free_document", "player_note"):
        return {"formatted": {"content": raw_content, "_raw": raw_content}}

    schema = _FORMAT_SCHEMAS.get(entry_type)
    if not schema:
        # Unknown type → safe passthrough rather than blowing up the save flow.
        return {"formatted": {"content": raw_content, "_raw": raw_content}}

    guidance = _FORMAT_GUIDANCE.get(entry_type, "")
    system_prompt = (
        "You convert free-form coaching text into a structured JSON record.\n"
        "Return ONLY valid JSON matching this exact schema:\n"
        f"{schema}\n\n"
        "EXTRACTION RULES:\n"
        "1. The schema field names are buckets — values you write inside MUST be "
        "in the SAME LANGUAGE as the source text (Hebrew text → Hebrew values, "
        "English text → English values). Do not translate.\n"
        "2. Be GENEROUS in mapping content to fields. If a paragraph is about "
        "defensive principles, put it in 'defense' even if the text doesn't say "
        "the word 'defense' literally. If the text describes pick-and-roll "
        "coverage, the scenario is 'pick-and-roll' and the steps go into "
        "'defense' or 'key_principles'. Use your judgment to route content into "
        "the most appropriate bucket.\n"
        "3. It is BETTER to put borderline content in 'notes' or 'key_principles' "
        "than to leave everything empty. An empty record is a failure.\n"
        "4. Only leave a field as empty string when there is genuinely no content "
        "for it (e.g. no opponent name was mentioned).\n"
        "5. Numbers should be integers if mentioned (0 if not). Arrays default "
        "to []. Objects default to {}.\n"
        "6. Do not invent specific facts (player names, dates, scores) that are "
        "not in the text. But DO route the text's actual content into fields.\n"
        "7. FORMATTING — when a field naturally contains MULTIPLE distinct items "
        "(several drills, multiple principles, sequential sections), separate "
        "each item with a SINGLE NEWLINE (\\n) so the coach sees each item on "
        "its own line. Example for practice_plan.main_part:\n"
        "  \"עבודה על יסודות: ... מטרה: ... עצימות: בינונית.\\n"
        "טכניקה אישית: ... מטרה: ... עצימות: בינונית.\\n"
        "עקרונות קבוצתיים: ... מטרה: ... עצימות: בינונית.\"\n"
        "Do NOT use newlines inside a single item — keep each drill/principle "
        "on one line. This applies to all compound fields (warmup, main_part, "
        "cooldown, offense, defense, key_principles, etc.)."
    )
    if guidance:
        system_prompt += f"\n\nFIELD GUIDANCE:\n{guidance}"

    try:
        client = get_client()
        resp = await client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": raw_content},
            ],
        )
        # Cost log: open a fresh short-lived session AFTER the LLM call so we
        # never hold a SQLite write lock during the slow OpenAI round-trip.
        # Best-effort — a logging failure must not break the save flow.
        try:
            async with AsyncSessionLocal() as db_log:
                await log_response(
                    db_log, resp,
                    user_id=user.id, team_id=user.active_team_id,
                    agent_key="notebook_format", endpoint="notebook",
                )
                await db_log.commit()
        except Exception as log_exc:
            logger.warning("[notebook/format] log_response failed: %s", log_exc)

        result = (resp.choices[0].message.content or "").strip()
        # Strip fenced code blocks if the model wrapped its JSON in ```...```.
        if result.startswith("```"):
            result = result.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        formatted = _json.loads(result)
        if isinstance(formatted, dict):
            # Safety net: always include _raw so the renderer can fall back to
            # the original text if the structured extraction missed everything.
            formatted["_raw"] = raw_content
        return {"formatted": formatted}
    except Exception as e:
        logger.warning("Notebook format-for-save error: %s", e)
        return {
            "formatted": {"content": raw_content, "_raw": raw_content},
            "warning": "Could not auto-format content. Raw content preserved.",
        }


# NOTE: route order matters in FastAPI — literal paths (`/stats`,
# `/attendance`, `/player/{...}`, `/game-stats/*`, `/team-*`, `/match-players`)
# MUST be registered BEFORE the parameterized `/{entry_id}` routes.
# Otherwise a GET to `/stats` would match `/{entry_id}` and be rejected
# with 422 because "stats" isn't an int. The /{entry_id} GET/PUT/DELETE
# handlers live near the end of the file for that reason.

async def _load_entry(
    db: AsyncSession, entry_id: int, user_id: int
) -> NotebookEntry | None:
    """Fetch + tenant-check in one round trip. Returns None if missing OR
    not owned by `user_id`."""
    return (await db.execute(
        select(NotebookEntry).where(
            NotebookEntry.id == entry_id,
            NotebookEntry.user_id == user_id,
        )
    )).scalar_one_or_none()


async def _attendance_dicts(db: AsyncSession, entry_id: int) -> list[dict]:
    rows = (await db.execute(
        select(NotebookAttendance).where(NotebookAttendance.entry_id == entry_id)
    )).scalars().all()
    return [
        {"player_id": a.player_id, "status": a.status, "note": a.note}
        for a in rows
    ]


async def _entry_player_ids(db: AsyncSession, entry_id: int) -> list[int]:
    rows = (await db.execute(
        select(NotebookEntryPlayer.player_id)
        .where(NotebookEntryPlayer.entry_id == entry_id)
        .order_by(NotebookEntryPlayer.player_id)
    )).all()
    return [r[0] for r in rows]


class _EntryUpdateBody(BaseModel):
    title: str | None = None
    entry_date: str | None = None
    content: Any | None = None
    player_id: int | None = None
    source: str | None = None
    tags: list[str] | None = None
    player_ids: list[int] | None = None
    attendance: list[dict] | None = None


# Placeholder — the actual @router.get/put/delete("/{entry_id}") decorators
# are at the bottom of the file (after all literal paths). The handlers
# themselves live here for organisational clarity.

async def _get_entry_handler(
    entry_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    entry = await _load_entry(db, entry_id, user.id)
    if not entry:
        raise HTTPException(status_code=404, detail="Not found")
    attendance = (
        await _attendance_dicts(db, entry_id)
        if entry.entry_type == "attendance"
        else []
    )
    player_ids = await _entry_player_ids(db, entry_id)
    return {
        "success": True,
        "data": _serialize_entry(entry, attendance=attendance, player_ids=player_ids),
    }


async def _update_entry_handler(
    entry_id: int,
    body: _EntryUpdateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    entry = await _load_entry(db, entry_id, user.id)
    if not entry:
        raise HTTPException(status_code=404, detail="Not found")

    data = body.model_dump(exclude_unset=True)

    if "title" in data:
        entry.title = (data["title"] or "").strip()
    if "entry_date" in data:
        entry.entry_date = data["entry_date"]
    if "source" in data:
        entry.source = data["source"]
    if "player_id" in data:
        entry.player_id = int(data["player_id"]) if data["player_id"] else None
    if "content" in data:
        c = data["content"]
        entry.content_json = c if isinstance(c, dict) else {"content": c}
    if "tags" in data:
        entry.tags_json = data["tags"] or []
    entry.updated_at = datetime.utcnow().isoformat()

    # Attendance: only re-applied for attendance-type entries when the
    # caller actually passed new records (matches v1 semantics).
    if entry.entry_type == "attendance" and "attendance" in data:
        await db.execute(
            delete(NotebookAttendance).where(NotebookAttendance.entry_id == entry_id)
        )
        for rec in (data["attendance"] or []):
            db.add(NotebookAttendance(
                entry_id=entry_id,
                player_id=int(rec["player_id"]),
                status=rec.get("status", "present"),
                note=rec.get("note") or "",
            ))

    # M-M player tagging — only touch if the caller sent player_ids OR
    # player_id (matches v1 service.update_entry behavior — a title-only
    # update mustn't clobber existing tags).
    if "player_ids" in data or "player_id" in data:
        combined = list(data.get("player_ids") or [])
        single = data.get("player_id")
        if single:
            try:
                combined.append(int(single))
            except (TypeError, ValueError):
                pass
        await db.execute(
            delete(NotebookEntryPlayer).where(NotebookEntryPlayer.entry_id == entry_id)
        )
        seen: set[int] = set()
        for pid in combined:
            if pid in seen:
                continue
            seen.add(pid)
            db.add(NotebookEntryPlayer(entry_id=entry_id, player_id=pid))

    await db.flush()

    attendance = (
        await _attendance_dicts(db, entry_id)
        if entry.entry_type == "attendance" else []
    )
    pids = await _entry_player_ids(db, entry_id)
    return {
        "success": True,
        "data": _serialize_entry(entry, attendance=attendance, player_ids=pids),
    }


async def _delete_entry_handler(
    entry_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    entry = await _load_entry(db, entry_id, user.id)
    if not entry:
        raise HTTPException(status_code=404, detail="Not found")
    await db.execute(delete(NotebookEntry).where(NotebookEntry.id == entry_id))
    await db.flush()
    return {"success": True}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/stats")
async def stats(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user.active_team_id is None:
        return {"success": True, "data": {"counts": {}, "total": 0}}
    rows = (await db.execute(
        select(NotebookEntry.entry_type, func.count())
        .where(
            NotebookEntry.user_id == user.id,
            NotebookEntry.team_id == user.active_team_id,
        )
        .group_by(NotebookEntry.entry_type)
    )).all()
    counts = {r[0]: int(r[1]) for r in rows}
    return {"success": True, "data": {"counts": counts, "total": sum(counts.values())}}


# ---------------------------------------------------------------------------
# Player notes — entries that mention a player (legacy or M-M)
# ---------------------------------------------------------------------------

@router.get("/player/{player_id}")
async def player_notes(
    player_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user.active_team_id is None:
        return {"success": True, "data": []}
    join_subq = select(NotebookEntryPlayer.entry_id).where(
        NotebookEntryPlayer.player_id == player_id
    )
    stmt = (
        select(NotebookEntry)
        .where(
            NotebookEntry.user_id == user.id,
            NotebookEntry.team_id == user.active_team_id,
        )
        .where(
            (NotebookEntry.player_id == player_id) |
            NotebookEntry.id.in_(join_subq)
        )
        .order_by(
            NotebookEntry.entry_date.desc(),
            NotebookEntry.created_at.desc(),
        )
    )
    rows = list((await db.execute(stmt)).scalars().all())
    # DISTINCT-by-id (an entry could match both paths above)
    seen: set[int] = set()
    out: list[dict] = []
    for r in rows:
        if r.id in seen:
            continue
        seen.add(r.id)
        out.append(_serialize_entry(r))
    return {"success": True, "data": out}


# ---------------------------------------------------------------------------
# Attendance summary
# ---------------------------------------------------------------------------

@router.get("/attendance")
async def attendance_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    player_id: int | None = None,
) -> dict:
    if user.active_team_id is None:
        return {"success": True, "data": {}}
    sql = (
        "SELECT a.player_id, a.status, COUNT(*) AS cnt "
        "FROM notebook_attendance a "
        "JOIN notebook_entries e ON e.id = a.entry_id "
        "WHERE e.user_id = :uid AND e.team_id = :tid"
    )
    params: dict = {"uid": user.id, "tid": user.active_team_id}
    if player_id is not None:
        sql += " AND a.player_id = :pid"
        params["pid"] = int(player_id)
    sql += " GROUP BY a.player_id, a.status"
    rows = (await db.execute(text(sql), params)).mappings().all()

    summary: dict[int, dict[str, int]] = {}
    for r in rows:
        pid = int(r["player_id"])
        bucket = summary.setdefault(
            pid,
            {"present": 0, "absent": 0, "late": 0, "injured": 0, "excused": 0, "total": 0},
        )
        bucket[r["status"]] = int(r["cnt"])
        bucket["total"] += int(r["cnt"])
    return {"success": True, "data": summary}


# ---------------------------------------------------------------------------
# Game stats — save / fetch / aggregate
# ---------------------------------------------------------------------------

class _GameStatsBody(BaseModel):
    """Input shape from the box-score importer / manual stats form."""

    game_date: str | None = None
    opponent: str | None = ""
    venue: str | None = ""
    score_us: int | None = 0
    score_them: int | None = 0
    quarter_scores: list[dict] | None = None
    what_worked: str | None = ""
    what_didnt: str | None = ""
    standout_players: str | None = ""
    next_practice_focus: str | None = ""
    players: list[dict] = Field(default_factory=list)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@router.post("/game-stats", status_code=201)
async def save_game_stats(
    body: _GameStatsBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Persist per-player box scores. Auto-creates a game_summary notebook
    entry that the existing notebook UI renders. Mirrors v1 service.save_game_stats
    byte-for-byte (score backfill from quarters, top-performer pick, etc)."""
    team_id = _require_team(user)
    if not body.players:
        return {"success": True, "data": {"saved": 0}}

    now = datetime.utcnow().isoformat()
    game_date = body.game_date or date.today().isoformat()
    opponent = (body.opponent or "").strip()

    total_pts = sum(_safe_int(p.get("points")) for p in body.players)
    score_us = _safe_int(body.score_us, total_pts)
    score_them = _safe_int(body.score_them)

    quarter_scores: list[dict[str, int]] = []
    for q in body.quarter_scores or []:
        if isinstance(q, dict):
            quarter_scores.append({
                "us": _safe_int(q.get("us")), "them": _safe_int(q.get("them"))
            })

    if quarter_scores:
        sum_us = sum(q["us"] for q in quarter_scores)
        sum_them = sum(q["them"] for q in quarter_scores)
        if score_us == 0 and sum_us > 0:
            score_us = sum_us
        if score_them == 0 and sum_them > 0:
            score_them = sum_them

    venue = (body.venue or "").strip().lower()
    if venue not in ("home", "away", ""):
        venue = ""

    if score_us > score_them:
        result = "W"
    elif score_them > score_us:
        result = "L"
    elif score_us == score_them and (score_us or score_them):
        result = "T"
    else:
        result = ""

    top_performer = ""
    if body.players:
        top = max(body.players, key=lambda p: _safe_int(p.get("points")))
        if _safe_int(top.get("points")) > 0:
            top_performer = (top.get("name") or "").strip()

    title_score = f" {score_us}-{score_them}" if score_us or score_them else ""
    title = (
        f"vs {opponent}{title_score}" if opponent
        else f"Game — {game_date}{title_score}"
    ).strip()

    entry = NotebookEntry(
        user_id=user.id, team_id=team_id, entry_type="game_summary",
        title=title, entry_date=game_date, source="stats_import",
        content_json={
            "opponent": opponent, "venue": venue,
            "score_us": score_us, "score_them": score_them,
            "result": result,
            "quarter_scores": quarter_scores,
            "top_performer": top_performer,
            "what_worked": (body.what_worked or "").strip(),
            "what_didnt": (body.what_didnt or "").strip(),
            "standout_players": (body.standout_players or "").strip(),
            "next_practice_focus": (body.next_practice_focus or "").strip(),
            "notes": f"Imported from box score · {len(body.players)} players",
        },
        tags_json=[],
        created_at=now, updated_at=now,
    )
    db.add(entry)
    await db.flush()

    saved = 0
    for p in body.players:
        pid = p.get("player_id")
        if not pid:
            continue
        # Skip rows with this game already recorded (idempotent on
        # (player_id, game_date, opponent) — same as v1 INSERT OR IGNORE).
        exists = (await db.execute(
            select(PlayerGameStat.id).where(
                PlayerGameStat.player_id == int(pid),
                PlayerGameStat.game_date == game_date,
                PlayerGameStat.opponent == opponent,
            )
        )).scalar_one_or_none()
        if exists:
            continue
        row = PlayerGameStat(
            user_id=user.id, team_id=team_id,
            player_id=int(pid), notebook_entry_id=entry.id,
            game_date=game_date, opponent=opponent,
            **{c: _safe_int(p.get(c)) for c in _STAT_COLS},
        )
        db.add(row)
        saved += 1

    await db.flush()
    return {
        "success": True,
        "data": {"saved": saved, "notebook_entry_id": entry.id},
    }


@router.get("/game-stats/player/{player_id}")
async def player_game_stats(
    player_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    if user.active_team_id is None:
        return {"success": True, "data": []}
    rows = (await db.execute(
        select(PlayerGameStat)
        .where(
            PlayerGameStat.user_id == user.id,
            PlayerGameStat.team_id == user.active_team_id,
            PlayerGameStat.player_id == player_id,
        )
        .order_by(PlayerGameStat.game_date.desc())
        .limit(limit)
    )).scalars().all()
    return {
        "success": True,
        "data": [
            {**{c: getattr(r, c) for c in _STAT_COLS},
             "id": r.id, "game_date": r.game_date, "opponent": r.opponent,
             "player_id": r.player_id, "notebook_entry_id": r.notebook_entry_id}
            for r in rows
        ],
    }


@router.get("/game-stats/player/{player_id}/summary")
async def player_stats_summary(
    player_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user.active_team_id is None:
        return {"success": True, "data": None}

    sums_select = ", ".join(f"SUM({c}) AS sum_{c}" for c in _STAT_COLS)
    avgs_select = ", ".join(f"AVG({c}) AS avg_{c}" for c in _STAT_COLS)
    sql = (
        f"SELECT COUNT(*) AS games, {sums_select}, {avgs_select} "
        f"FROM player_game_stats "
        f"WHERE user_id = :u AND team_id = :t AND player_id = :p"
    )
    row = (await db.execute(text(sql), {"u": user.id, "t": user.active_team_id, "p": player_id})).mappings().one_or_none()
    if not row or not row["games"]:
        return {"success": True, "data": None}

    d: dict[str, Any] = {"games": int(row["games"])}
    for c in _STAT_COLS:
        d[f"sum_{c}"] = int(row[f"sum_{c}"] or 0)
        avg = row[f"avg_{c}"]
        d[f"avg_{c}"] = round(float(avg), 1) if avg is not None else 0.0
    return {"success": True, "data": d}


@router.get("/game-stats/entry/{entry_id}")
async def game_stats_by_entry(
    entry_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (await db.execute(
        text(
            "SELECT s.*, p.name AS player_name, p.number AS player_number "
            "FROM player_game_stats s LEFT JOIN players p ON p.id = s.player_id "
            "WHERE s.user_id = :u AND s.notebook_entry_id = :e "
            "ORDER BY s.points DESC"
        ),
        {"u": user.id, "e": entry_id},
    )).mappings().all()
    return {"success": True, "data": [dict(r) for r in rows]}


@router.delete("/game-stats/{stat_id}")
async def delete_game_stat(
    stat_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stat = (await db.execute(
        select(PlayerGameStat).where(
            PlayerGameStat.id == stat_id,
            PlayerGameStat.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not stat:
        raise HTTPException(status_code=404, detail="Not found")
    await db.execute(delete(PlayerGameStat).where(PlayerGameStat.id == stat_id))
    await db.flush()
    return {"success": True}


@router.get("/team-stats")
async def team_stats_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user.active_team_id is None:
        return {"success": True, "data": None}
    games = int((await db.execute(
        text(
            "SELECT COUNT(DISTINCT game_date || '|' || opponent) "
            "FROM player_game_stats WHERE user_id = :u AND team_id = :t"
        ),
        {"u": user.id, "t": user.active_team_id},
    )).scalar() or 0)
    if games == 0:
        return {"success": True, "data": None}

    sums_select = ", ".join(f"SUM({c}) AS sum_{c}" for c in _STAT_COLS)
    row = (await db.execute(
        text(
            f"SELECT {sums_select} FROM player_game_stats "
            f"WHERE user_id = :u AND team_id = :t"
        ),
        {"u": user.id, "t": user.active_team_id},
    )).mappings().one()

    d: dict[str, Any] = {"games": games}
    for c in _STAT_COLS:
        total = int(row[f"sum_{c}"] or 0)
        d[f"sum_{c}"] = total
        d[f"avg_{c}"] = round(total / games, 1) if games else 0
    return {"success": True, "data": d}


@router.get("/team-leaders")
async def team_leaders(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user.active_team_id is None:
        return {"success": True, "data": {"scoring": [], "rebounds": [], "assists": []}}

    leaders: dict[str, list[dict]] = {}
    for stat, label in [("points", "scoring"), ("reb", "rebounds"), ("ast", "assists")]:
        rows = (await db.execute(
            text(
                f"SELECT s.player_id, p.name, p.number, "
                f"ROUND(AVG(s.{stat}), 1) AS avg_val, COUNT(*) AS gp "
                f"FROM player_game_stats s LEFT JOIN players p ON p.id = s.player_id "
                f"WHERE s.user_id = :u AND s.team_id = :t "
                f"GROUP BY s.player_id, p.name, p.number "
                f"ORDER BY avg_val DESC LIMIT 3"
            ),
            {"u": user.id, "t": user.active_team_id},
        )).mappings().all()
        leaders[label] = [dict(r) for r in rows]
    return {"success": True, "data": leaders}


# ---------------------------------------------------------------------------
# Player matching (used by the box-score importer to align names → roster)
# ---------------------------------------------------------------------------

@router.post("/match-players")
async def match_players(
    body: dict = Body(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Greedy similarity scoring: exact > number > contains > token-overlap.
    Matches v1 service.match_players_to_roster ordering so the importer
    behaves identically."""
    names = body.get("names") or []
    if not names:
        raise HTTPException(status_code=400, detail="Names list required")
    if user.active_team_id is None:
        return {"success": True, "data": []}

    players = list((await db.execute(
        select(Player).where(
            Player.user_id == user.id,
            Player.team_id == user.active_team_id,
            Player.active.is_(True),
        )
    )).scalars().all())

    results: list[dict] = []
    for raw_name in names:
        name = (raw_name or "").strip()
        name_lower = name.lower()
        best, best_conf = None, 0.0
        name_tokens = set(name_lower.split())

        for p in players:
            p_name = (p.name or "").lower()
            p_number = str(p.number or "")

            if name_lower == p_name:
                best, best_conf = p, 1.0
                break

            stripped = name_lower.replace("#", "").strip()
            if stripped == p_number and p_number:
                best, best_conf = p, 0.95
                break

            if name_lower in p_name or p_name in name_lower:
                if len(name_lower) >= 2 and 0.8 > best_conf:
                    best, best_conf = p, 0.8

            p_tokens = set(p_name.split())
            overlap = name_tokens & p_tokens
            if overlap:
                conf = len(overlap) / max(len(name_tokens), len(p_tokens)) * 0.85
                if conf > best_conf:
                    best, best_conf = p, conf

        results.append({
            "name": name,
            "player_id": best.id if best else None,
            "matched_name": best.name if best else None,
            "confidence": round(best_conf, 2),
        })
    return {"success": True, "data": results}


# ---------------------------------------------------------------------------
# /{entry_id} routes — registered LAST so the literal-path routes above
# (`/stats`, `/attendance`, `/player/...`, `/game-stats/...`, `/team-...`,
# `/match-players`) get a chance to match first. Otherwise FastAPI's first-
# match-wins routing would funnel everything into `/{entry_id}` and reject
# `/stats` with 422.
# ---------------------------------------------------------------------------

router.get("/{entry_id}")(_get_entry_handler)
router.put("/{entry_id}")(_update_entry_handler)
router.delete("/{entry_id}")(_delete_entry_handler)
