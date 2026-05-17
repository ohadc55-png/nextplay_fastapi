"""Phase 15 — auto-assigned team colors for the Coach Calendar.

The month grid distinguishes events from different teams by a colored chip.
Coaches don't pick colors manually (per user spec: "כל קבוצה מקבלת צבע
אוטומטי"); we hand each team a palette slot deterministically from its
primary key, so the same team always renders in the same color even
across page loads / devices / shared calendar URLs.

The palette is a curated set of 8 hues that read well on the Coach App's
dark surface (#0f1520). Hand-picked: not red-or-green pairs that fail for
common color-blindness, no near-blacks that vanish against the bg, and
each is bright enough to act as a 24-32px chip.

Determinism: `palette_for_team(team_id) → "#hex"` is pure — no DB read.
For backfill onto existing rows that have `color_hex IS NULL`, callers
call `ensure_team_color(db, team)` which writes the resolved color back
so subsequent reads are stable even if the palette is ever reordered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.models.teams import TeamProfile


# 8 distinct hues, all >= 60% lightness against the #0f1520 bg.
# Order matters — adding/reordering changes which slot a team lands on for
# rows still using lazy-assignment. Once `color_hex` is persisted, the
# stored value is the source of truth and palette changes don't shift it.
PALETTE: tuple[str, ...] = (
    "#ff6b35",  # 0 — orange ember (matches --accent)
    "#5b9eff",  # 1 — sky blue
    "#7ddf6f",  # 2 — fresh green
    "#ffd166",  # 3 — sunny yellow
    "#c084fc",  # 4 — soft violet
    "#fb7185",  # 5 — coral pink
    "#22d3ee",  # 6 — cyan
    "#a3e635",  # 7 — lime
)


def palette_for_team(team_id: int) -> str:
    """Return the hex color this team should render in.

    Deterministic from `team_id` alone. The modulo rotates after 8 teams —
    visual collisions for orgs with >8 teams are intentional and accepted
    (per plan).
    """
    if not isinstance(team_id, int) or team_id < 1:
        # Fallback to the accent color for any malformed id; never raises.
        return PALETTE[0]
    return PALETTE[team_id % len(PALETTE)]


async def ensure_team_color(
    db: AsyncSession, team: TeamProfile
) -> str:
    """Resolve `team.color_hex`, writing the palette default if missing.

    Mutates `team` in place + flushes; commit is the caller's job (matches
    repo convention — services flush, route deps commit). Idempotent: a
    team that already has a `color_hex` is returned as-is, no write.

    Returns the resolved hex string.
    """
    if team.color_hex:
        return team.color_hex
    team.color_hex = palette_for_team(team.id)
    await db.flush()
    return team.color_hex


__all__ = ["PALETTE", "palette_for_team", "ensure_team_color"]
