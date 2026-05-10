"""Onboarding service — port of v1 backend/api/onboarding.py.

Powers two flows:

1. **The home-page roadmap** — a 12-item, 3-stage checklist showing the
   coach how far they've come. Each item reads completion off a real
   DB signal (a row in `team_profile` / `players` / `conversations`
   / `uploads` / `plays` / `notebook_entries` / `scouting_videos` /
   `video_clips`) or a row in `onboarding_events` for "first-use"
   features that don't persist their own data.

2. **Brad-led player profiling** — when the coach lands on
   `/chat?agent=gm&onboarding=scouting`, Brad walks them through every
   un-profiled player. The chat service injects an
   `ONBOARDING_SCOUTING` context block into Brad's system prompt
   (built by `build_onboarding_scouting_context`). After each turn, the
   `extract_player_skills` helper runs the LLM on the coach's
   description + any existing CSV notes, parses the resulting JSON
   into 21 numeric metrics + refined strengths/weaknesses + a one-line
   scout summary, and persists everything.

Mirrors v1 exactly so behavior parity is preserved (master prompt
"the One Rule"). Async-port: every DB call goes through the FastAPI
session, every LLM call uses the async OpenAI client. The CSV-import
background extraction (`bulk_add_players` in v1) runs as a FastAPI
`BackgroundTask` instead of a daemon thread.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.crew.llm import get_client, log_response
from src.models.analytics import OnboardingEvent
from src.models.conversations import Conversation
from src.models.notebook import NotebookEntry
from src.models.players import Player, PlayerMetric
from src.models.plays import Play
from src.models.scouting import ScoutingVideo, VideoClip
from src.models.teams import TeamProfile
from src.models.uploads import Upload

logger = logging.getLogger(__name__)


# 21 metric keys across 6 categories — must match
# frontend/templates/player.html. Same order as v1 to keep the chat
# extraction prompt drop-in compatible.
PLAYER_METRIC_KEYS: tuple[str, ...] = (
    "shooting", "ball_handling", "passing", "driving", "offensive_iq",
    "on_ball_defense", "help_defense", "defensive_iq", "rebounding",
    "court_vision", "decision_making", "coachability",
    "competitiveness", "teamwork", "composure",
    "system_execution", "transition_play", "spacing_movement",
    "speed_agility", "strength_toughness", "stamina",
)

# Allowed `event` values for /api/onboarding/mark-event. Adding new
# events requires updating this set + the home-page JS that fires them.
ALLOWED_EVENTS: frozenset[str] = frozenset({
    "play_creator_used",
    "video_editor_used",
})

# File types that count as "season stats" for the Stage-3 unlock.
_STAT_FILE_TYPES: tuple[str, ...] = ("csv", "xlsx", "xls", "pdf")

# Model for the player-skills extraction. Matches v1 byte-for-byte.
_EXTRACT_MODEL = "gpt-5.4-mini"

EXTRACTION_SYSTEM_PROMPT = """You are an expert basketball scout building a player profile from coach inputs.

You will receive TWO sources of information:
1. EXISTING NOTES — what the coach already wrote about the player (typically from CSV upload: strengths, weaknesses, free-form notes).
2. NEW DESCRIPTION — what the coach is now adding via chat.

Your job is to combine BOTH sources and produce a refined professional scouting profile.

Return ONLY a single valid JSON object. No markdown fences. No commentary. No surrounding text.

Schema (use EXACTLY these keys):
{
  "skills": {
    "shooting": null|1-10, "ball_handling": null|1-10, "passing": null|1-10, "driving": null|1-10, "offensive_iq": null|1-10,
    "on_ball_defense": null|1-10, "help_defense": null|1-10, "defensive_iq": null|1-10, "rebounding": null|1-10,
    "court_vision": null|1-10, "decision_making": null|1-10, "coachability": null|1-10,
    "competitiveness": null|1-10, "teamwork": null|1-10, "composure": null|1-10,
    "system_execution": null|1-10, "transition_play": null|1-10, "spacing_movement": null|1-10,
    "speed_agility": null|1-10, "strength_toughness": null|1-10, "stamina": null|1-10
  },
  "strengths": "<refined comma-separated list — see rules>",
  "weaknesses": "<refined comma-separated list — see rules>",
  "scout_summary": "<one sentence professional summary>",
  "missing_info": ["<skill key not yet addressed>", ...]
}

Rules:

SKILLS:
- Score 1-10: 1=very weak, 5=average, 10=elite. Use null when not addressed in EITHER source.
- Use BOTH existing notes and new description as evidence. Be conservative — only score what is clearly stated.

STRENGTHS / WEAKNESSES (this is your refined output, NOT a copy):
- Synthesize ALL information you have into a clean, concise list.
- These will REPLACE the existing strengths/weaknesses in the database — make them better than what was there.
- Highlight what stands out based on the metrics + the coach's words combined.
- Keep them short — 3-5 short phrases each, comma-separated.

LANGUAGE:
- Detect the dominant language of the EXISTING notes. If they exist, write your output in that language.
- If existing notes are empty, use the language of the NEW description.
- Strengths, weaknesses, and scout_summary MUST all be in the same single language.

SCOUT_SUMMARY:
- One sentence, professional tone, captures the player's identity (e.g., "An aggressive slasher and on-ball defender who needs to develop a reliable jumper.").

MISSING_INFO:
- List 2-4 skill keys (from the schema) that neither source addressed. Helps the next follow-up question.

Never invent attributes. Only use what the coach told you (in either source).
"""


# ---------------------------------------------------------------------------
# Roadmap status — 3 stages × 4 items = 12 checklist entries
# ---------------------------------------------------------------------------


async def compute_onboarding_status(
    db: AsyncSession, *, user_id: int, team_id: int,
) -> dict[str, Any]:
    """Build the dict consumed by the home-page widget. 12 items across
    3 stages; each item carries its own CTA label + href so the JS can
    render without hard-coded strings. Mirrors v1's `_compute_status`."""

    async def _count(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    # ── Stage 1: Get Started ───────────────────────────────────────
    team_setup = await _count(
        select(func.count())
        .select_from(TeamProfile)
        .where(
            TeamProfile.id == team_id,
            TeamProfile.team_name.is_not(None),
            func.length(func.trim(TeamProfile.team_name)) > 0,
            TeamProfile.league.is_not(None),
            func.length(func.trim(TeamProfile.league)) > 0,
        )
    ) >= 1

    player_count = await _count(
        select(func.count())
        .select_from(Player)
        .where(Player.team_id == team_id, Player.active.is_(True))
    )
    three_players = player_count >= 3
    full_roster = player_count >= 8

    first_chat = await _count(
        select(func.count())
        .select_from(Conversation)
        .where(
            Conversation.team_id == team_id,
            Conversation.user_id == user_id,
            Conversation.role == "user",
        )
    ) >= 1

    first_file = await _count(
        select(func.count())
        .select_from(Upload)
        .where(Upload.team_id == team_id, Upload.user_id == user_id)
    ) >= 1

    # ── Stage 2: Build Your Playbook ───────────────────────────────
    plays_saved = await _count(
        select(func.count())
        .select_from(Play)
        .where(Play.team_id == team_id, Play.user_id == user_id)
    )
    play_creator_event = await _count(
        select(func.count())
        .select_from(OnboardingEvent)
        .where(
            OnboardingEvent.team_id == team_id,
            OnboardingEvent.user_id == user_id,
            OnboardingEvent.event == "play_creator_used",
        )
    )
    first_play = (plays_saved >= 1) or (play_creator_event >= 1)

    scout_chat = await _count(
        select(func.count())
        .select_from(Conversation)
        .where(
            Conversation.team_id == team_id,
            Conversation.user_id == user_id,
            Conversation.agent_used == "scout",
        )
    ) >= 1

    first_notebook = await _count(
        select(func.count())
        .select_from(NotebookEntry)
        .where(NotebookEntry.team_id == team_id, NotebookEntry.user_id == user_id)
    ) >= 1

    # ── Stage 3: Coach Like a Pro ──────────────────────────────────
    video_upload = await _count(
        select(func.count())
        .select_from(ScoutingVideo)
        .where(ScoutingVideo.team_id == team_id, ScoutingVideo.user_id == user_id)
    ) >= 1

    # Video edit counts saved clips OR a "first-use" editor open event.
    clips_saved = await _count(
        select(func.count())
        .select_from(VideoClip)
        .join(ScoutingVideo, VideoClip.video_id == ScoutingVideo.id)
        .where(ScoutingVideo.team_id == team_id, ScoutingVideo.user_id == user_id)
    )
    video_editor_event = await _count(
        select(func.count())
        .select_from(OnboardingEvent)
        .where(
            OnboardingEvent.team_id == team_id,
            OnboardingEvent.user_id == user_id,
            OnboardingEvent.event == "video_editor_used",
        )
    )
    video_edit = (clips_saved >= 1) or (video_editor_event >= 1)

    practice_plan = await _count(
        select(func.count())
        .select_from(NotebookEntry)
        .where(
            NotebookEntry.team_id == team_id,
            NotebookEntry.user_id == user_id,
            NotebookEntry.entry_type == "practice_plan",
        )
    ) >= 1

    season_stats = await _count(
        select(func.count())
        .select_from(Upload)
        .where(
            Upload.team_id == team_id,
            Upload.user_id == user_id,
            func.lower(Upload.file_type).in_(_STAT_FILE_TYPES),
        )
    ) >= 1

    stages = [
        {
            "id": "stage1",
            "title": "Get Started",
            "subtitle": "Your team is ready to work",
            "items": [
                {"id": "team_setup", "label": "Set up your team name & league",
                 "cta": "Team Setup", "href": "/team-setup", "done": team_setup},
                {"id": "three_players", "label": "Add your first 3 players",
                 "cta": "Add Players", "href": "/team-setup#roster", "done": three_players},
                {"id": "first_chat", "label": "Have your first chat with your GM",
                 "cta": "Start Chat", "href": "/chat?agent=gm", "done": first_chat},
                {"id": "first_file", "label": "Upload your first file for AI analysis",
                 "cta": "Upload File", "href": "/chat", "done": first_file},
            ],
        },
        {
            "id": "stage2",
            "title": "Build Your Playbook",
            "subtitle": "Your team comes alive",
            "items": [
                {"id": "full_roster", "label": "Complete your roster",
                 "cta": "Edit Roster", "href": "/team-setup#roster", "done": full_roster},
                {"id": "first_play", "label": "Create your first play in Play Creator",
                 "cta": "Open Play Creator", "href": "/plays", "done": first_play},
                {"id": "scout_chat", "label": "Chat with Jack about an opponent",
                 "cta": "Talk to Jack", "href": "/chat?agent=scout", "done": scout_chat},
                {"id": "first_notebook", "label": "Save your first Notebook entry",
                 "cta": "Open Notebook", "href": "/notebook", "done": first_notebook},
            ],
        },
        {
            "id": "stage3",
            "title": "Coach Like a Pro",
            "subtitle": "You're ready for game day",
            "items": [
                {"id": "video_upload", "label": "Upload a game/practice to Video Hub",
                 "cta": "Open Video Hub", "href": "/scouting", "done": video_upload},
                {"id": "video_edit", "label": "Edit your first video",
                 "cta": "Edit Video", "href": "/scouting", "done": video_edit},
                {"id": "practice_plan", "label": "Generate a full practice plan from chat",
                 "cta": "Ask Duncan", "href": "/chat?agent=training", "done": practice_plan},
                {"id": "season_stats", "label": "Upload your full season stats — unlock deep analytics",
                 "cta": "Upload Stats", "href": "/chat?agent=analytics", "done": season_stats},
            ],
        },
    ]

    total_items = sum(len(s["items"]) for s in stages)
    total_done = sum(1 for s in stages for it in s["items"] if it["done"])
    return {
        "all_done": total_done == total_items,
        "total_done": total_done,
        "total_items": total_items,
        "stages": stages,
    }


# ---------------------------------------------------------------------------
# Mark first-use event (idempotent)
# ---------------------------------------------------------------------------


async def mark_event(
    db: AsyncSession, *, user_id: int, team_id: int, event: str,
) -> bool:
    """Insert one row in `onboarding_events`. Idempotent via the
    `(user_id, team_id, event)` UNIQUE constraint — the second call is
    a no-op. Returns True on success (whether or not the row was new),
    False on bad input."""
    if event not in ALLOWED_EVENTS:
        return False

    # Use SQLite's INSERT OR IGNORE for portability — the same
    # statement also works on Postgres because asyncpg accepts
    # `ON CONFLICT DO NOTHING` via the dialect's `on_conflict_do_nothing`.
    # We branch on dialect to keep the Postgres path explicit.
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(OnboardingEvent).values(
            user_id=user_id, team_id=team_id, event=event,
            first_seen=datetime.utcnow().isoformat(),
        ).on_conflict_do_nothing(
            index_elements=["user_id", "team_id", "event"],
        )
    else:
        stmt = sqlite_insert(OnboardingEvent).values(
            user_id=user_id, team_id=team_id, event=event,
            first_seen=datetime.utcnow().isoformat(),
        ).prefix_with("OR IGNORE")

    await db.execute(stmt)
    return True


# ---------------------------------------------------------------------------
# Players profiling status (used by Brad to know who's done / next)
# ---------------------------------------------------------------------------


async def players_profiling_status(
    db: AsyncSession, *, user_id: int, team_id: int,
) -> dict[str, Any]:
    """Return `{profiled: [...], pending: [...], next: {...} or None}`.

    A player is `profiled` once `metrics_filled_at` is non-null
    (which `extract_player_skills` sets after a successful chat-led
    extraction). Order: jersey number ascending, NULL numbers last,
    then by id."""
    rows = (await db.execute(
        select(
            Player.id, Player.name, Player.number, Player.position,
            Player.metrics_filled_at, Player.scout_summary,
        )
        .where(Player.team_id == team_id, Player.active.is_(True))
        .order_by(Player.number.is_(None), Player.number, Player.id)
    )).all()

    profiled: list[dict] = []
    pending: list[dict] = []
    for r in rows:
        item = {
            "id": r.id, "name": r.name, "number": r.number,
            "position": r.position,
        }
        if r.metrics_filled_at:
            item["scout_summary"] = r.scout_summary or ""
            profiled.append(item)
        else:
            pending.append(item)

    return {
        "profiled": profiled,
        "pending": pending,
        "next": pending[0] if pending else None,
    }


# ---------------------------------------------------------------------------
# Player skill extraction (LLM)
# ---------------------------------------------------------------------------


async def extract_player_skills(
    db: AsyncSession,
    *,
    user_id: int,
    team_id: int,
    player_id: int,
    description: str,
    set_metrics_filled_at: bool = True,
) -> dict[str, Any]:
    """Run the LLM-extract pipeline on one player's description + prior
    notes. Persists merged metrics + refined text fields. Returns a
    `{success, ...}` dict matching v1's contract.

    Args:
        player_id: must belong to `team_id` (caller responsible for ownership)
        description: coach's free-text. Empty is allowed when the
            player already has CSV notes (initial-CSV path).
        set_metrics_filled_at: True for chat-led extraction (player
            counts as "profiled"). False for the bulk CSV-import
            initial pass — leaves `metrics_filled_at` NULL so Brad
            still walks through the player.
    """
    player = (await db.execute(
        select(Player).where(Player.id == player_id, Player.team_id == team_id)
    )).scalar_one_or_none()
    if player is None:
        return {"success": False, "error": "Player not found"}

    # Build the existing-notes block. Brad sees this so he can
    # acknowledge what the coach already wrote (CSV) and avoid asking
    # questions the answer is already in.
    existing_parts: list[str] = []
    if player.strengths:
        existing_parts.append(f"- Strengths: {player.strengths}")
    if player.weaknesses:
        existing_parts.append(f"- Weaknesses: {player.weaknesses}")
    if player.notes:
        existing_parts.append(f"- Notes: {player.notes}")
    existing_block = "\n".join(existing_parts) if existing_parts else (
        "(none — no prior notes from coach)"
    )

    # No data at all → nothing to extract. Mirrors v1's early-out so
    # the frontend can surface a sensible error.
    if not description and not existing_parts:
        return {"success": False, "error": "no_data",
                "detail": "Nothing to extract from"}

    bio_parts: list[str] = []
    if player.position:
        bio_parts.append(f"position {player.position}")
    if player.height:
        bio_parts.append(f"height {player.height}")
    if player.weight:
        bio_parts.append(f"weight {player.weight}")
    if player.age:
        bio_parts.append(f"age {player.age}")
    bio_str = ", ".join(bio_parts) or "no bio data"

    user_msg = (
        f"PLAYER: {player.name or ''} "
        f"(jersey #{player.number or '?'}, {bio_str}).\n\n"
        f"EXISTING NOTES (from coach's prior input — typically the CSV upload):\n"
        f"{existing_block}\n\n"
        f"NEW DESCRIPTION (coach is now adding this via chat):\n"
        f"{description or '(none yet — derive what you can from the existing notes only)'}\n\n"
        f"Combine both sources into a refined scouting profile per the schema."
    )

    try:
        client = get_client()
        resp = await client.chat.completions.create(
            model=_EXTRACT_MODEL,
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=800,
        )
    except Exception as e:  # noqa: BLE001 — LLM API errors are diverse
        logger.exception("[onboarding] LLM extraction call failed: %s", e)
        return {"success": False, "error": "extraction_failed",
                "detail": "AI service unavailable"}

    try:
        await log_response(
            db, resp,
            user_id=user_id, team_id=team_id,
            agent_key="onboarding_player_extract",
            endpoint="onboarding",
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[onboarding] cost-log skipped: %s", e)

    raw = (resp.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[onboarding] extraction returned invalid JSON: %r", raw[:200])
        return {"success": False, "error": "extraction_failed",
                "detail": "Could not parse response"}

    # Sanitize: keep only known keys, only valid 1-10 ints.
    raw_skills = parsed.get("skills") if isinstance(parsed.get("skills"), dict) else {}
    metrics: dict[str, int] = {}
    for key in PLAYER_METRIC_KEYS:
        val = raw_skills.get(key)
        if val is None:
            continue
        try:
            ival = int(val)
            if 1 <= ival <= 10:
                metrics[key] = ival
        except (ValueError, TypeError):
            continue

    strengths = str(parsed.get("strengths") or "").strip()[:500]
    weaknesses = str(parsed.get("weaknesses") or "").strip()[:500]
    scout_summary = str(parsed.get("scout_summary") or "").strip()[:500]
    raw_missing = parsed.get("missing_info") if isinstance(parsed.get("missing_info"), list) else []
    missing_info = [str(x)[:50] for x in raw_missing if x][:6]

    # Merge new metrics on top of any existing ones (CSV-derived
    # values stay unless the chat overrides them with a new score).
    existing_metric_row = (await db.execute(
        select(PlayerMetric).where(PlayerMetric.player_id == player_id)
    )).scalar_one_or_none()

    existing_metrics: dict = {}
    if existing_metric_row and existing_metric_row.metrics_json:
        if isinstance(existing_metric_row.metrics_json, dict):
            existing_metrics = dict(existing_metric_row.metrics_json)
        elif isinstance(existing_metric_row.metrics_json, str):
            try:
                existing_metrics = json.loads(existing_metric_row.metrics_json)
            except (json.JSONDecodeError, ValueError):
                existing_metrics = {}

    merged_metrics = {**existing_metrics, **metrics}

    if merged_metrics:
        if existing_metric_row is None:
            db.add(PlayerMetric(
                player_id=player_id,
                user_id=user_id, team_id=team_id,
                metrics_json=merged_metrics,
                updated_at=datetime.utcnow().isoformat(),
            ))
        else:
            existing_metric_row.metrics_json = merged_metrics
            existing_metric_row.updated_at = datetime.utcnow().isoformat()

    # Patch player text fields — only overwrite when the LLM gave us
    # something non-empty.
    if strengths:
        player.strengths = strengths
    if weaknesses:
        player.weaknesses = weaknesses
    if scout_summary:
        player.scout_summary = scout_summary
    if set_metrics_filled_at:
        player.metrics_filled_at = datetime.utcnow()

    await db.flush()

    return {
        "success": True,
        "player_id": player_id,
        "metrics_extracted": metrics,
        "merged_metrics": merged_metrics,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "scout_summary": scout_summary,
        "missing_info": missing_info,
    }


# ---------------------------------------------------------------------------
# Brad's ONBOARDING_SCOUTING context block (injected into system prompt)
# ---------------------------------------------------------------------------


async def build_onboarding_scouting_context(
    db: AsyncSession, *, team_id: int,
) -> str:
    """Return the context block Brad's system prompt expects when the
    coach lands on `?onboarding=scouting`. Mirrors v1's
    `_build_onboarding_scouting_context` byte-for-byte: lists profiled
    + pending players, surfaces the first pending player's CSV notes
    as the focus, and tunes verbosity based on progress."""
    rows = (await db.execute(
        select(
            Player.id, Player.name, Player.number, Player.position,
            Player.strengths, Player.weaknesses, Player.notes,
            Player.metrics_filled_at,
        )
        .where(Player.team_id == team_id, Player.active.is_(True))
        .order_by(Player.number.is_(None), Player.number, Player.id)
    )).all()
    if not rows:
        return ""

    profiled: list[str] = []
    pending: list[str] = []
    pending_full: list[dict] = []
    for r in rows:
        label = f"#{r.number} {r.name}" if r.number else (r.name or "")
        if r.position:
            label += f" ({r.position})"
        if r.metrics_filled_at:
            profiled.append(label)
        else:
            pending.append(label)
            pending_full.append({
                "name": r.name, "number": r.number, "position": r.position,
                "strengths": r.strengths, "weaknesses": r.weaknesses,
                "notes": r.notes,
            })

    if not pending:
        return (
            "ONBOARDING_SCOUTING context:\n"
            f"- All {len(profiled)} players have been profiled. "
            "Acknowledge completion warmly and offer to keep chatting normally about anything else."
        )

    next_player = pending_full[0]
    next_label = pending[0]
    profiled_count = len(profiled)

    if profiled_count < 3:
        verbosity = (
            "VERBOSITY: We're early in the walkthrough. Show the coach you're actually reading "
            "their inputs — reference what's already in the CSV/existing notes for this player, "
            "confirm the update explicitly, and then move to the next player. Build trust."
        )
    else:
        verbosity = (
            "VERBOSITY: Coach knows you've got it by now. Keep it tight — a one-line acknowledgment "
            "is enough. Example: 'Locked in. Now <next player>?' Don't recap the CSV every time."
        )

    existing_bits: list[str] = []
    if next_player.get("strengths"):
        existing_bits.append(f"already-noted strengths: \"{next_player['strengths']}\"")
    if next_player.get("weaknesses"):
        existing_bits.append(f"already-noted weaknesses: \"{next_player['weaknesses']}\"")
    if next_player.get("notes"):
        existing_bits.append(f"notes: \"{next_player['notes']}\"")
    existing_str = "; ".join(existing_bits) if existing_bits else (
        "no prior notes — start fresh"
    )

    return "\n".join([
        "ONBOARDING_SCOUTING context (system-injected — not coach-visible):",
        f"- Players already profiled ({profiled_count}): "
        f"{', '.join(profiled) if profiled else 'none yet'}",
        f"- Players awaiting profiling ({len(pending)}): {', '.join(pending)}",
        f"- Current focus: {next_label}",
        f"- For the current focus player — {existing_str}",
        "- After the coach describes the current player, the system extracts metrics "
        "in the background. You don't need to extract; just acknowledge and move on.",
        verbosity,
    ])


__all__ = [
    "ALLOWED_EVENTS",
    "EXTRACTION_SYSTEM_PROMPT",
    "PLAYER_METRIC_KEYS",
    "build_onboarding_scouting_context",
    "compute_onboarding_status",
    "extract_player_skills",
    "mark_event",
    "players_profiling_status",
]
