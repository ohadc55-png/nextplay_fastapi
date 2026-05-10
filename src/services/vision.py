"""GPT-4o Vision pipeline — async port of the v1 two-stage flow.

The pipeline:

  Stage 1 — describe_basketball_image:
    GPT-4o Vision call with a strict extraction prompt that returns
    "IMAGE_TYPE: ..." plus structured factual scene observations.
    No interpretation, no persona, no opinions — just facts.

  Stage 2 — build_two_stage_enriched_message:
    Wraps Stage 1's output + a type-aware instruction block + the
    coach's question into a single user message. The specialist agent
    (Brad / Hunter / Nexus / Vance / Williams) processes that message
    through its own persona to deliver the actual analysis.

  Fallback — analyze_image:
    Single-call Vision with the agent's persona as the system prompt.
    Used when Stage 1 fails (rare — file IO or OpenAI outage).

Why two stages? GPT-4o is best at OBJECTIVE scene extraction with a
strict prompt; specialist personas drift / hallucinate when given the
image directly. Splitting the work — extractor extracts, specialist
synthesizes — gives a sharp scene description AND the right voice in
the response. This is mirror-port from `backend/file_processor.py`
274-559 in v1.0-flask.

Wiring: this module is consumed by the upcoming `/api/chat-upload`
endpoint (Phase 7). For now it's a self-contained library + tests so
the vision logic ships before the file processor lands.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re

from src.crew.llm import get_client, log_response

logger = logging.getLogger(__name__)


IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp"})


def get_file_extension(filename_or_path: str) -> str:
    """Return the lowercased extension WITHOUT the leading dot, or empty."""
    if not filename_or_path:
        return ""
    base = os.path.basename(filename_or_path)
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def is_image(filename: str) -> bool:
    return get_file_extension(filename) in IMAGE_EXTENSIONS


def _mime_type(filepath: str) -> str:
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }.get(get_file_extension(filepath), "image/jpeg")


# ---------------------------------------------------------------------------
# Game stats JSON tail (verbatim from v1 backend/file_processor.py:203)
# ---------------------------------------------------------------------------

GAME_STATS_JSON_HINT = (
    "\n\nIMPORTANT: If this image/file contains player game statistics (box score / stat sheet), "
    "you MUST do two things:\n"
    "1. Show a readable summary for the coach\n"
    "2. At the END of your response, output a JSON block wrapped EXACTLY like this:\n\n"
    "EXTRACTION SEQUENCE — DO ALL FIVE STEPS IN ORDER. NEVER STOP EARLY.\n"
    "  STEP 1: GAME HEADER → extract opponent, game_date, venue from the top of the sheet.\n"
    "  STEP 2: PLAYER TABLE → fill the players[] array (one entry per player row).\n"
    "  STEP 3: TEAM TOTALS row → confirm score_us via the team total of points.\n"
    "  STEP 4: SCORE BY QUARTERS table (usually at the BOTTOM in its own small table).\n"
    "          From this table extract BOTH quarter_scores[] and score_them.\n"
    "  STEP 5: SELF-CHECK before writing the JSON — if score_us > 0 but score_them = 0,\n"
    "          you missed STEP 4. Don't ship a JSON with score_them = 0 against any\n"
    "          non-zero score_us — opponents essentially never score 0.\n\n"
    "[GAME_STATS_JSON]{"
        '"game_date":"YYYY-MM-DD",'
        '"opponent":"TEAM",'
        '"venue":"home|away|",'
        '"score_us":0,'
        '"score_them":0,'
        '"quarter_scores":[{"us":0,"them":0},{"us":0,"them":0},{"us":0,"them":0},{"us":0,"them":0}],'
        '"what_worked":"2-3 sentences on what your team did well",'
        '"what_didnt":"2-3 sentences on what broke down",'
        '"standout_players":"Names + brief why (use stats)",'
        '"next_practice_focus":"1-2 sentence recommendation",'
        '"players":[{"name":"...","minutes":0,"points":0,"fgm":0,"fga":0,"three_pm":0,"three_pa":0,"ftm":0,"fta":0,"oreb":0,"dreb":0,"reb":0,"ast":0,"stl":0,"blk":0,"turnovers":0,"pf":0,"plus_minus":0}]'
    "}[/GAME_STATS_JSON]"
)


# ---------------------------------------------------------------------------
# Stage 1 — extraction prompts (verbatim from v1 file_processor.py:289-379)
# ---------------------------------------------------------------------------

_EXTRACTOR_SYSTEM_PROMPT = (
    "You are a basketball scene extractor. Your ONLY job is to report what is "
    "visually present in the image — facts, not interpretation. Another agent "
    "will do the tactical analysis. You provide the ground truth they work from."
)


def _build_extraction_prompt(user_message: str) -> str:
    """The full extraction-prompt body — IMAGE_TYPE tag + per-type sections."""
    return (
        "You are extracting basketball-specific facts for another agent to analyze tactically. "
        "Your output MUST be concrete and usable — NOT a generic description of a basketball photo.\n\n"
        "THE VERY FIRST LINE of your response MUST be exactly one of:\n"
        "IMAGE_TYPE: STAT_SHEET        (a box score, stat table, spreadsheet of numbers)\n"
        "IMAGE_TYPE: GAME_SCENE        (photo/screenshot of actual gameplay — live action, players on court)\n"
        "IMAGE_TYPE: PLAY_DIAGRAM      (drawn play, X's and O's, whiteboard, coach's diagram with arrows)\n"
        "IMAGE_TYPE: SHOT_CHART        (shot chart, heatmap, player movement chart)\n"
        "IMAGE_TYPE: OTHER             (anything else)\n\n"
        "==== IF GAME_SCENE — FOLLOW THIS EXACT STRUCTURE ====\n\n"
        "DO NOT write generic comments like \"live game photo, action near the hoop, crowd in background, "
        "FIBA branding visible\". THAT IS USELESS for tactical analysis. Skip event branding, skip crowd, "
        "skip arena. Focus on PLAYERS and POSITIONS.\n\n"
        "Required sections (numbered, concrete, in this order):\n\n"
        "1. PLAYER COUNT & JERSEYS\n"
        "   - Count every player visible. State: \"X players in light jerseys, Y players in dark jerseys\"\n"
        "   - Which team appears to have the ball (offense)?\n"
        "   - List visible jersey numbers if readable.\n\n"
        "2. COURT ZONES & POSITIONS (be specific per player)\n"
        "   Use these zone names: paint, low post, mid post, elbow (left/right), wing (left/right), "
        "corner (left/right), top of key, above the break, below the FT line, at the rim.\n"
        "   Format: \"Light #7 — driving from right wing toward rim\", \"Dark #4 — help position in paint\".\n\n"
        "3. BALL LOCATION\n"
        "   Exactly where the ball is and the action stage (pass, drive, finish attempt, rebound contest, etc.).\n\n"
        "4. OFFENSIVE ALIGNMENT\n"
        "   Spacing and formation: 4-out 1-in / 5-out / horns / stack / isolation / post-up / pick-and-roll.\n\n"
        "5. DEFENSIVE ALIGNMENT\n"
        "   Man-to-man vs zone (2-3, 3-2, 1-3-1, 1-2-2, matchup), press, pack-line, ice, switch, hedge.\n\n"
        "6. KEY READS & MATCHUPS\n"
        "   - Help-side positioning, mismatches, open shooters, closeout quality, box-out positioning.\n\n"
        "7. ONE-LINE SCENE SUMMARY\n"
        "   A single sentence summarizing the tactical moment.\n\n"
        "==== IF PLAY_DIAGRAM ====\n"
        "Describe every X, O, arrow, screen mark, dotted line, and label. Transcribe play names exactly.\n\n"
        "==== IF STAT_SHEET ====\n"
        "Transcribe in this EXACT order — every section is mandatory:\n\n"
        "  GAME HEADER:\n"
        "    Format: 'GAME HEADER: [our team] vs [opponent], date=[YYYY-MM-DD], venue=[home/away], league=[name]'\n\n"
        "  TEAM ROSTERS (CRITICAL when the sheet shows 2 teams):\n"
        "    Format: 'TEAM ROSTERS:\n"
        "       Team A ([full team name]): [name1], [name2], ...\n"
        "       Team B ([full team name]): [name1], [name2], ...'\n"
        "    If single-team: 'TEAM ROSTERS: single-team sheet'.\n\n"
        "  PLAYER TABLE: every row, every column, every number, prefixed '[Team A: PlayerName]'.\n\n"
        "  TEAM TOTALS rows — for EACH team separately:\n"
        "    'TEAM TOTALS Team A ([name]): PTS=X, 2P%=X, 3P%=X, FT%=X, REB=X, AST=X, ...'\n\n"
        "  SCORE BY QUARTERS table:\n"
        "    Format: 'SCORE BY QUARTERS: [Team A] Q1=X Q2=X Q3=X Q4=X Total=X | [Team B] ...'\n"
        "    If absent: 'SCORE BY QUARTERS: not visible'.\n\n"
        "==== IF SHOT_CHART ====\n"
        "Transcribe EVERY name, number, label, and zone.\n\n"
        "==== IF OTHER ====\n"
        "Describe what you see factually.\n\n"
        "OUTPUT RULES: plain text, numbered sections. No opinions. No words like "
        "\"should\", \"better\", \"weakness\", \"opportunity\". Only what is visible.\n\n"
        f"{f'COACH ASKED: {user_message}' if user_message else ''}"
    )


# ---------------------------------------------------------------------------
# Stage 2 — type-aware specialist instructions (verbatim from v1:425-484)
# ---------------------------------------------------------------------------

_STAGE2_INSTRUCTIONS: dict[str, str] = {
    "GAME_SCENE": (
        "This is a LIVE GAME PHOTO / SCREENSHOT of actual gameplay. The coach wants "
        "TACTICAL ANALYSIS — not a description, not stats, not caveats about missing data.\n\n"
        "STRICT RULES — DO NOT VIOLATE:\n"
        "- FORBIDDEN openings: \"This is a live game photo\", \"not a box score\", \"no stats to extract\", "
        "\"data not available\", \"can't extract\", \"without more context\".\n"
        "- FORBIDDEN topics: event branding, crowd, \"live game vs training\".\n"
        "- FORBIDDEN meta: do NOT describe what an image \"is\" — just DO the analysis.\n\n"
        "REQUIRED OUTPUT (no preamble, get straight to basketball):\n"
        "1. WHAT THE OFFENSE IS DOING — 1-2 sentences using real basketball vocabulary "
        "(drive-and-kick, horns, PnR, post-up, baseline cut, weak-side action, transition, DHO, etc.)\n"
        "2. WHAT THE DEFENSE IS DOING — coverage, rotation, help position, closeouts, matchups\n"
        "3. THE KEY READ — what the ball-handler should see / what the defense just gave up\n"
        "4. 2-4 ACTIONABLE TAKEAWAYS — concrete coaching points\n\n"
        "If the visual analysis is thin, say \"from what's visible\" and STILL deliver concrete "
        "tactical reads. NEVER say the image has no tactical value.\n\n"
        "Respond ONLY in the same language the coach wrote in. Apply your persona and voice."
    ),
    "PLAY_DIAGRAM": (
        "This is a PLAY DIAGRAM / WHITEBOARD with X's and O's. The coach wants "
        "you to break it down — the concept, the reads, what it attacks, how to defend "
        "(or how to run) it. NO stat extraction. NO \"no data\".\n\n"
        "Cover: the action/concept, the primary read, counters if defense X/Y, "
        "personnel fit for our roster (use TEAM CONTEXT), how to practice it.\n\n"
        "Respond ONLY in the same language the coach wrote in. Apply your persona and voice."
    ),
    "SHOT_CHART": (
        "This is a SHOT CHART / HEATMAP. Interpret the hot zones, cold zones, "
        "shot selection patterns, and what it implies about this player/team's "
        "tendencies and defensive scheme against them.\n\n"
        "Respond ONLY in the same language the coach wrote in. Apply your persona and voice."
    ),
    "STAT_SHEET": (
        "This is a STAT SHEET / BOX SCORE the coach JUST UPLOADED.\n\n"
        "ABSOLUTE RULE — DATA SOURCE ISOLATION:\n"
        "The VISUAL ANALYSIS above is the SOLE SOURCE OF TRUTH for THIS game's metadata.\n"
        "  - opponent / game_date / venue → ONLY from the GAME HEADER line\n"
        "  - score_us / score_them / quarter_scores → ONLY from SCORE BY QUARTERS or team totals\n"
        "  - players[] → ONLY from the PLAYER TABLE rows\n"
        "DO NOT pull metadata from TEAM CONTEXT, prior uploads, or memory.\n"
        "DO NOT call query_team_db or search_kb for this game's metadata.\n"
        "If a field is 'unknown' / 'not visible', write '' in your JSON — never fabricate.\n\n"
        "Use TEAM CONTEXT only to match player names in the PLAYER TABLE to roster IDs (spelling).\n\n"
        "Now: extract the data, give a readable summary, flag standout numbers, add insight."
        + GAME_STATS_JSON_HINT
    ),
    "OTHER": (
        "Describe what you see using the visual analysis above, and offer relevant "
        "coaching insight tied to the coach's question.\n\n"
        "Respond ONLY in the same language the coach wrote in. Apply your persona and voice."
    ),
}


def _detect_image_type(scene_description: str) -> str:
    """Parse the IMAGE_TYPE tag from Stage 1 output. Defaults to GAME_SCENE
    when missing — that's the most common photo upload."""
    m = re.search(
        r"IMAGE_TYPE:\s*(STAT_SHEET|GAME_SCENE|PLAY_DIAGRAM|SHOT_CHART|OTHER)",
        scene_description or "",
        re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()
    return "GAME_SCENE"


def build_two_stage_enriched_message(
    scene_description: str, user_message: str
) -> str:
    """Stage 2 input: wraps the Stage 1 description + type-aware instruction
    block + coach's question into one user message ready for the specialist."""
    image_type = _detect_image_type(scene_description)
    instruction = _STAGE2_INSTRUCTIONS.get(image_type, _STAGE2_INSTRUCTIONS["GAME_SCENE"])
    return (
        f"UPLOADED IMAGE — VISUAL ANALYSIS (pre-extracted from the image):\n"
        f"{scene_description}\n\n"
        f"COACH'S QUESTION: {user_message}\n\n"
        f"Treat the visual analysis above as ground truth — do NOT say you cannot see the image.\n\n"
        f"{instruction}"
    )


# ---------------------------------------------------------------------------
# Image loading helper (sync — wrapped in asyncio.to_thread by callers)
# ---------------------------------------------------------------------------


def _load_image_data_uri_sync(filepath: str) -> str:
    """Read the image off disk and return a data: URI.
    Sync IO; wrap in asyncio.to_thread when called from async code."""
    with open(filepath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{_mime_type(filepath)};base64,{b64}"


# ---------------------------------------------------------------------------
# Public API — async
# ---------------------------------------------------------------------------


async def describe_basketball_image(
    filepath: str,
    user_message: str = "",
    *,
    db=None,
    user_id: int | None = None,
    team_id: int | None = None,
) -> str:
    """Stage 1: Vision extracts an OBJECTIVE scene description.

    Cost is logged via `log_response` when `db` is provided (the upload
    handler will pass it; tests can call without and skip logging).
    Raises on failure — the caller (chat-upload handler) is expected to
    catch and fall back to `analyze_image`."""
    data_uri = await asyncio.to_thread(_load_image_data_uri_sync, filepath)
    extraction_prompt = _build_extraction_prompt(user_message)

    client = get_client()
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _EXTRACTOR_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": extraction_prompt},
                {"type": "image_url", "image_url": {
                    "url": data_uri,
                    "detail": "high",
                }},
            ]},
        ],
        max_tokens=1500,
        temperature=0.1,
    )
    if db is not None:
        try:
            await log_response(
                db, response,
                user_id=user_id, team_id=team_id,
                agent_key=None, endpoint="vision-extract",
            )
        except Exception as e:
            logger.debug("[vision] cost log skipped: %s", e)

    content = (response.choices[0].message.content or "")
    logger.info(
        "[vision] Stage 1 extract (first 400 chars): %s",
        content[:400].replace("\n", " | "),
    )
    return content


async def analyze_image(
    filepath: str,
    *,
    agent_prompt: str,
    user_message: str,
    team_ctx: str = "",
    db=None,
    user_id: int | None = None,
    team_id: int | None = None,
) -> str:
    """Single-call Vision fallback. Used when Stage 1 fails — feeds the
    image directly to a Vision call with the agent's persona as the
    system prompt. Returns a friendly error string on failure (never
    raises) so the chat handler always has something to surface."""
    try:
        data_uri = await asyncio.to_thread(_load_image_data_uri_sync, filepath)
    except Exception as e:
        logger.exception("[vision] could not read image at %s", filepath)
        return f"Error reading image: {e}"

    user_text = (
        f"{f'TEAM CONTEXT:{chr(10)}{team_ctx}{chr(10)}{chr(10)}' if team_ctx else ''}"
        f"COACH'S MESSAGE: {user_message}\n\n"
        "The coach uploaded this image. You MUST analyze it — DO NOT say you cannot see or read it.\n"
        "Look carefully at the image and extract ALL visible information.\n\n"
        "If it contains statistics, tables, or numbers:\n"
        "- Extract EVERY number, name, and stat you can read\n"
        "- Reproduce the data in an organized format\n"
        "- Provide analysis and insights based on the extracted data\n\n"
        "If it shows a basketball court, play diagram, or game situation:\n"
        "- Identify player positions, formations, spacing\n"
        "- Analyze defensive/offensive alignment, mismatches, tactical opportunities\n\n"
        "Be specific, data-driven, and actionable. Respond in the same language the coach uses."
        f"{GAME_STATS_JSON_HINT}"
    )

    try:
        client = get_client()
        # v1 uses gpt-5.4-mini for analyze_image (the single-call
        # fallback path) — see backend/file_processor.py:544. Stage 1
        # describe still uses gpt-4o because it needs Vision.
        response = await client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[
                {"role": "system", "content": agent_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {
                        "url": data_uri,
                        "detail": "high",
                    }},
                ]},
            ],
            max_tokens=2000,
            temperature=0.3,
        )
        if db is not None:
            try:
                await log_response(
                    db, response,
                    user_id=user_id, team_id=team_id,
                    agent_key=None, endpoint="vision",
                )
            except Exception as e:
                logger.debug("[vision] cost log skipped: %s", e)
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.exception("[vision] Vision API analysis failed")
        return f"Error analyzing image: {e}"


__all__ = [
    "GAME_STATS_JSON_HINT",
    "_STAGE2_INSTRUCTIONS",
    "_detect_image_type",
    "analyze_image",
    "build_two_stage_enriched_message",
    "describe_basketball_image",
    "get_file_extension",
    "is_image",
]
