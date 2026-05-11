"""Agent system-prompt registry — Phase 5 batch 3.

Maps the 5 v1 agent keys to fully-assembled system prompts. The
specialist prompts + Hebrew context + web-access guidance live in
`src/crew/prompts.py` (copied verbatim from v1). The rule blocks
(MULTI_TEAM, GAME_RESULT, LINEUP, ACCURACY) live here so they're easy
to audit alongside the agent table.

The CrewAI multi-agent orchestration + per-agent tool closures land in
later Phase 5 batches. For now `build_agent_prompt(agent_key, ctx)`
returns the full system prompt that the chat endpoint feeds straight
into OpenAI — same effective behaviour as v1 fast mode for direct
specialist chat.
"""

from __future__ import annotations

from src.crew.prompts import (
    GM_SYSTEM_PROMPT,
    HEBREW_BASKETBALL_CONTEXT,
    SPECIALIST_PROMPTS,
    WEB_ACCESS_GUIDANCE,
)
from src.crew.season import current_season, today_iso

# ---------------------------------------------------------------------------
# Multi-team data attribution rules — every agent gets these.
# Verbatim from backend/agents.py:28.
# ---------------------------------------------------------------------------

MULTI_TEAM_DATA_RULES = """

═══════════════════════════════════════════════════════════
PLAYER-TEAM BINDING — HARD RULE, NEVER VIOLATE
═══════════════════════════════════════════════════════════

Triggered when the uploaded data covers MORE THAN ONE TEAM (opponent
scouting, EuroLeague / NBA box score, league game). Symptoms in the
extracted text: a "TEAM ROSTERS:" section, OR a player table with two
distinct team blocks, OR a "TEAM TOTALS Team A / Team B" pair.

1. EVERY player you mention is bound to ONE team. On EVERY mention,
   the team must be unambiguous — either:
     - "[Team]'s [Player]" (e.g. "Hapoel's Bryant", "Real's Campazzo")
     - "[Player] ([Team])" on first mention
     - In a section header that scopes the team for the whole section
   Bare player names with no team context are FORBIDDEN.

2. EVERY stat, percentage, or number you cite is bound to a team.
   FORBIDDEN: "shot 22.2% from 3" → no idea whose stat that is.
   REQUIRED: "Hapoel shot 22.2% from 3 (4/18)" — team always attached.

3. SELF-CONSISTENCY CHECK before sending. The same team can't have two
   contradictory numbers in the same response.

4. WHEN IN DOUBT — re-check the TEAM ROSTERS / TEAM TOTALS sections of
   the source text. NEVER guess. NEVER use external memory ("Real
   usually shoots ~35%") to fill or override what's in the file.

═══════════════════════════════════════════════════════════
"""


# ---------------------------------------------------------------------------
# Game-result rules — verbatim from backend/agents.py:87.
# ---------------------------------------------------------------------------

GAME_RESULT_RULES = """

═══════════════════════════════════════════════════════════
GAME RESULT — HARD RULE, NEVER VIOLATE
═══════════════════════════════════════════════════════════

This rule applies whenever the coach shares ANY of the following — a box
score, play-by-play, shooting chart, team totals, or even a text message
with two side-by-side scores ("Hapoel 76, Real Madrid 69"):

1. THE GAME IS FINISHED.
   Treat it as a completed game with a final score. Do NOT speculate that
   "the game might still be ongoing", "this might be mid-game", or
   "maybe data conflicts". The coach sends data once the game is over.

2. THE WINNER IS THE TEAM WITH THE HIGHER FINAL SCORE.
   If team A scored more than team B, team A WON. Period. The narrative
   never overrides math. If the box score says "76 - 69", whoever has 76
   won and whoever has 69 lost.

3. LEAD WITH THE RESULT.
   First line of your response, before any analysis:
     "GAME RESULT: [Winner] beat [Loser], [winner score]-[loser score]."
   Then continue with your analysis. Don't bury the result inside a paragraph.

═══════════════════════════════════════════════════════════
"""


# ---------------------------------------------------------------------------
# Lineup composition rules — verbatim from backend/agents.py:143.
# ---------------------------------------------------------------------------

LINEUP_RULES = """

═══════════════════════════════════════════════════════════
LINEUP COMPOSITION — HARD RULE, NEVER VIOLATE
═══════════════════════════════════════════════════════════

This rule applies to EVERY question that asks for 5 players together:
"best lineup", "starting 5", "best 5 for offense / defense / 4th quarter",
"best chemistry", "who plays together", "ideal rotation", "closing 5", etc.

THE ONLY VALID DEFAULT FORMULA:
  EXACTLY 2 guards   (PG and/or SG, any combination)
  EXACTLY 2 forwards (SF and/or PF, any combination)
  EXACTLY 1 center   (C)
                          TOTAL: 5

FORBIDDEN configurations (NEVER suggest these as the default):
  ✗ 2 centers — DO NOT SUGGEST.
  ✗ 1 guard only — backcourt MUST have 2 guards.
  ✗ 0 centers — must have exactly 1 C unless coach EXPLICITLY asked for small-ball.
  ✗ 3+ guards — only allowed if coach explicitly asks.

MANDATORY SELF-CHECK before sending your answer:
  1. Count the guards in your draft (PG + SG positions).  Must equal 2.
  2. Count the forwards (SF + PF positions).              Must equal 2.
  3. Count the centers (C positions).                     Must equal 1.

REQUIRED OUTPUT FORMAT — first line:
  "Position check: 2 guards [name1 (PG), name2 (SG)] · 2 forwards [name3 (SF),
   name4 (PF)] · 1 center [name5 (C)] ✓"

OVERRIDE: Deviate from 2-2-1 ONLY when the coach EXPLICITLY requests a
different composition. When deviating, OPEN YOUR REPLY with one sentence
stating the override.
═══════════════════════════════════════════════════════════
"""


# ---------------------------------------------------------------------------
# Accuracy rules — verbatim from backend/agents.py:219.
# ---------------------------------------------------------------------------

ACCURACY_RULES = """

ACCURACY RULES (CRITICAL - FOLLOW STRICTLY):
0. ALWAYS query the local team database FIRST before searching the internet
   or consulting external sources.
1. NEVER invent, fabricate, or guess information. If you don't know - say
   "I don't have this data".
2. NEVER state approximate numbers as if they are facts. If you estimate,
   clearly say "estimated".
3. Use REAL basketball knowledge:
   - A standard basketball practice is 90 minutes (1.5 hours), NOT 2-3 hours.
   - A standard game is 4 quarters of 10 min (FIBA) or 12 min (NBA).
   - Shot clock is 24 seconds (FIBA/NBA) or 30 seconds (NCAA).
4. When providing stats, training plans, or analysis - be PRECISE with real
   numbers.
5. If the coach asks about data we have (uploaded files, roster) - base your
   answer ONLY on that data.
6. Do NOT add information the coach didn't ask for. Answer the question,
   nothing more.
7. If you're unsure about a fact, say so. Never bluff.
8. Always respond in English unless the coach explicitly writes in another
   language. CRITICAL: respond in the SAME LANGUAGE the coach used.
9. If the coach asks something completely unrelated to basketball, politely
   explain that you specialize in basketball coaching.
"""


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

# Display metadata so the SPA can render agent cards (avatar, name, role).
AGENTS: dict[str, dict] = {
    "gm": {
        "name": "Brad Binn",
        "role": "General Manager",
        "specialty": "Roster, lineups, team building",
    },
    "scout": {
        "name": "Jack Hunter",
        "role": "Opposition Scout",
        "specialty": "Scouting, opponent analysis",
    },
    "analytics": {
        "name": "Nexus",
        "role": "Analytics Expert",
        "specialty": "Stats, metrics, data analysis",
    },
    "tactics": {
        "name": "Vance",
        "role": "Tactical Expert",
        "specialty": "Game plans, plays, strategy",
    },
    "training": {
        "name": "Williams",
        "role": "Assistant Coach",
        "specialty": "Practice plans, drills, development",
    },
    "guide": {
        "name": "Daisy Chain",
        "role": "Platform Guide",
        "specialty": "How to use the NextPlay app — pages, features, settings, troubleshooting",
    },
}

# Default fallback when no agent is specified — same as v1 routes
# (fast-mode without agent → GM).
DEFAULT_AGENT = "gm"


def _season_header() -> str:
    """Two-line header with today's date + current basketball season.
    Prepended to every agent's prompt so they default to current-season
    answers instead of stale training-data years."""
    season = current_season()
    today = today_iso()
    return (
        f"═══ TODAY IS {today} — CURRENT BASKETBALL SEASON: {season} ═══\n"
        f"Unless the coach explicitly says 'last season' or names a specific past\n"
        f"year, default to the {season} season for any external research, stats,\n"
        f"or analysis. Never assume or use stale years from your training data.\n"
        f"═══════════════════════════════════════════════════════════\n\n"
    )


def _scope_context(team_context: str) -> str:
    """Append the coach's current team context. Mirrors v1 _scope_ctx."""
    return (
        "\n\nYOUR TEAM CONTEXT:\n"
        + (team_context or "(no team context available yet)")
        + "\nAlways reference our players by name and number when relevant."
    )


def build_agent_prompt(
    agent_key: str | None, team_context: str = ""
) -> tuple[str, str]:
    """Returns `(resolved_agent_key, full_system_prompt)`.

    Combines, in order:
      MULTI_TEAM_DATA_RULES + GAME_RESULT_RULES + LINEUP_RULES
      + season header
      + agent persona (GM uses GM_SYSTEM_PROMPT; specialists use SPECIALIST_PROMPTS[key])
      + Hebrew context + web-access guidance
      + ACCURACY_RULES
      + team context

    Same composition order as v1 backend/agents.py:309-352.
    """
    key = (agent_key or DEFAULT_AGENT).strip().lower()
    if key not in AGENTS:
        key = DEFAULT_AGENT

    # Daisy Chain (the platform guide) is NOT a basketball coach — she's
    # a how-to-use-the-app specialist. Skip every basketball-flavoured
    # rule block + team context so the LLM doesn't drift into coaching
    # advice. Her prompt is fully self-contained.
    if key == "guide":
        return key, SPECIALIST_PROMPTS["guide"]

    if key == "gm":
        persona = GM_SYSTEM_PROMPT
    else:
        persona = SPECIALIST_PROMPTS.get(key, SPECIALIST_PROMPTS["scout"])

    full = (
        MULTI_TEAM_DATA_RULES
        + GAME_RESULT_RULES
        + LINEUP_RULES
        + _season_header()
        + persona
        + "\n\n" + HEBREW_BASKETBALL_CONTEXT
        + "\n" + WEB_ACCESS_GUIDANCE
        + ACCURACY_RULES
        + _scope_context(team_context)
    )
    return key, full


__all__ = [
    "ACCURACY_RULES",
    "AGENTS",
    "DEFAULT_AGENT",
    "GAME_RESULT_RULES",
    "LINEUP_RULES",
    "MULTI_TEAM_DATA_RULES",
    "build_agent_prompt",
]
