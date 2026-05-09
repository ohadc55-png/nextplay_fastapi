"""Single source of truth for the current basketball season.

When the calendar rolls over from one season to the next, ONLY this file
changes. All agents and prompts that need to know "what season are we in?"
import from here.

The rule is simple and explicit:
  - Aug 1 → switch to next season (e.g. on Aug 1, 2026 we're in 2026-27)
  - Most NBA / EuroLeague / NCAA seasons start in October-November,
    so Aug 1 gives a 2-month buffer for off-season questions to still
    map to the upcoming season the coach is preparing for.
  - The exact rollover date can be tuned later by changing
    SEASON_ROLLOVER_MONTH below — no other file needs to change.
"""

from datetime import date

# Calendar month at which we move to the next season (1=Jan, 12=Dec).
# Aug 1 is the working default; refine when product needs it.
SEASON_ROLLOVER_MONTH = 8


def _season_start_year(today: date | None = None) -> int:
    """Internal helper — the calendar year the current season started in."""
    today = today or date.today()
    if today.month >= SEASON_ROLLOVER_MONTH:
        return today.year
    return today.year - 1


def current_season(today: date | None = None) -> str:
    """Return the current basketball season as 'YYYY-YY' (e.g. '2025-26').

    Examples (with default Aug 1 rollover):
      - April 26, 2026  → '2025-26'  (still inside the 25-26 season)
      - August 1, 2026  → '2026-27'  (rolled over to new season)
      - November 5, 2026 → '2026-27'
    """
    start = _season_start_year(today)
    return f"{start}-{str(start + 1)[-2:]}"


def current_season_end_year(today: date | None = None) -> str:
    """Return the END year of the current season as a string (for URLs).

    sports-reference / basketball-reference use the end-year in their URLs:
      /cbb/schools/duke/men/2026.html  ← this is the 2025-26 season
    Returns '2026' when current_season() is '2025-26'.
    """
    return str(_season_start_year(today) + 1)


def previous_season(today: date | None = None) -> str:
    """Return the previous season as 'YYYY-YY'.

    Used when the coach explicitly asks for 'last season' or historical data.
    """
    start = _season_start_year(today) - 1
    return f"{start}-{str(start + 1)[-2:]}"


def today_iso(today: date | None = None) -> str:
    """Return today's date as an ISO string 'YYYY-MM-DD'.

    Injected into agent backstories so they can reference 'today' naturally.
    """
    return (today or date.today()).isoformat()
