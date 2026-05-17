"""Phase 12 — redistribute Sha'ar Shivyon teams across programs × regions.

Target shape:
  - 6 programs × 6 regions, each program spans every region.
  - 640 teams total.
  - Sha'ar Shivyon = 302, Sal-Tech = 148, others sum to 190.
  - Region totals scale to current geographic density.

Why this script exists: until Phase 12, a team's program was inferred from
Region.program_id (1 region → 1 program). Each region hosted teams from
only one program, which contradicted reality. After adding
TeamProfile.program_id we can decouple — but existing seed data still has
all 550 teams with NULL region_id + NULL program_id. This script:

  1. Loads programs + regions for the target org.
  2. Reassigns existing teams' region_id + program_id per the matrix below.
  3. Creates new teams to fill the gap up to 640.
  4. Decouples Region.program_id (sets to NULL) so the model now reflects
     that regions are shared geography, not program ownership.

Idempotent: re-running produces the same final state (any team not
matching the matrix gets reassigned; any deficit gets filled).

Usage:
    python scripts/redistribute_shaar_shivyon_teams.py
    python scripts/redistribute_shaar_shivyon_teams.py --org-slug shaar-shivyon
    python scripts/redistribute_shaar_shivyon_teams.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import AsyncSessionLocal
from src.models.organizations import Organization
from src.models.programs import Program
from src.models.regions import Region
from src.models.teams import TeamProfile

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("redistribute")

DEFAULT_ORG_SLUG = "shaar-shivyon"

# Per-program targets — keys = Program.slug. Total = 640.
PROGRAM_TARGETS: dict[str, int] = {
    "shaar-shivyon":         302,
    "sal-tech":              148,
    "boatot":                 55,
    "malkat-ha-salim":        50,
    "shavim-le-nitzachon":    45,
    "hotrim-le-hatzlacha":    40,
}

# Region totals — scaled from the demo seeder's geographic density so
# the dashboard's "teams by region" chart still reflects reality. Sum = 640.
REGION_TOTALS: dict[str, int] = {
    "מחוז מרכז":    196,
    "מחוז ירושלים": 126,
    "מחוז חיפה":    103,
    "מחוז שפלה":     91,
    "מחוז צפון":     68,
    "מחוז דרום":     56,
}

# Full allocation matrix (program × region). Each row sums to that
# program's target; each column sums to that region's total. Pre-computed
# so the script is deterministic and easy to audit.
MATRIX: dict[str, dict[str, int]] = {
    "shaar-shivyon":         {"מחוז מרכז": 92, "מחוז ירושלים": 60, "מחוז חיפה": 49, "מחוז שפלה": 43, "מחוז צפון": 32, "מחוז דרום": 26},
    "sal-tech":              {"מחוז מרכז": 45, "מחוז ירושלים": 29, "מחוז חיפה": 24, "מחוז שפלה": 21, "מחוז צפון": 16, "מחוז דרום": 13},
    "boatot":                {"מחוז מרכז": 17, "מחוז ירושלים": 11, "מחוז חיפה":  9, "מחוז שפלה":  8, "מחוז צפון":  6, "מחוז דרום":  4},
    "malkat-ha-salim":       {"מחוז מרכז": 15, "מחוז ירושלים": 10, "מחוז חיפה":  8, "מחוז שפלה":  7, "מחוז צפון":  5, "מחוז דרום":  5},
    "shavim-le-nitzachon":   {"מחוז מרכז": 14, "מחוז ירושלים":  9, "מחוז חיפה":  7, "מחוז שפלה":  6, "מחוז צפון":  5, "מחוז דרום":  4},
    "hotrim-le-hatzlacha":   {"מחוז מרכז": 13, "מחוז ירושלים":  7, "מחוז חיפה":  6, "מחוז שפלה":  6, "מחוז צפון":  4, "מחוז דרום":  4},
}

# Age-bracket pool used when synthesizing new team names so the dataset
# stays varied and recognizable as basketball.
AGE_BRACKETS = ["U10", "U12", "U14", "U16", "U18", "בוגרים", "נשים"]

# Place names used as suffix to age bracket. Picked to match the geographic
# region the team lands in so the name makes sense.
PLACE_BY_REGION: dict[str, list[str]] = {
    "מחוז מרכז":    ["תל אביב", "רמת גן", "פתח תקווה", "הרצליה", "רעננה", "כפר סבא", "ראשון לציון", "חולון", "בת ים", "רחובות"],
    "מחוז ירושלים": ["ירושלים", "מעלה אדומים", "ביתר עילית", "אבו גוש", "מבשרת ציון"],
    "מחוז חיפה":    ["חיפה", "קרית אתא", "קרית מוצקין", "קרית ביאליק", "טירת הכרמל", "נשר", "עכו", "קרית טבעון"],
    "מחוז שפלה":    ["מודיעין", "רמלה", "לוד", "גדרה", "יבנה", "נס ציונה"],
    "מחוז צפון":    ["נצרת", "טבריה", "צפת", "עפולה", "כרמיאל", "מעלות", "קצרין", "ראש פינה"],
    "מחוז דרום":    ["באר שבע", "אשדוד", "אשקלון", "דימונה", "ירוחם", "אילת", "מצפה רמון"],
}


async def _load_or_die(s: AsyncSession, org_slug: str):
    org = (await s.execute(select(Organization).where(Organization.slug == org_slug))).scalar_one_or_none()
    if org is None:
        raise SystemExit(f"Org with slug={org_slug!r} not found.")
    programs = {p.slug: p for p in (await s.execute(select(Program).where(Program.organization_id == org.id))).scalars().all()}
    regions = {r.name: r for r in (await s.execute(select(Region).where(Region.organization_id == org.id))).scalars().all()}
    missing_progs = set(PROGRAM_TARGETS) - set(programs)
    missing_regs = set(REGION_TOTALS) - set(regions)
    if missing_progs:
        raise SystemExit(f"Missing programs: {missing_progs}")
    if missing_regs:
        raise SystemExit(f"Missing regions: {missing_regs}")
    return org, programs, regions


def _validate_matrix() -> None:
    """Defensive: make sure the hand-built MATRIX still sums correctly.
    A human-edited table is the most likely thing to silently drift."""
    for slug, target in PROGRAM_TARGETS.items():
        actual = sum(MATRIX[slug].values())
        if actual != target:
            raise SystemExit(f"Program {slug!r}: matrix sums to {actual}, expected {target}")
    for region, total in REGION_TOTALS.items():
        actual = sum(row[region] for row in MATRIX.values())
        if actual != total:
            raise SystemExit(f"Region {region!r}: matrix sums to {actual}, expected {total}")
    grand = sum(sum(row.values()) for row in MATRIX.values())
    if grand != sum(PROGRAM_TARGETS.values()):
        raise SystemExit(f"Grand total mismatch: {grand} != {sum(PROGRAM_TARGETS.values())}")


async def _flat_assignments(programs, regions) -> list[tuple[int, int, str, str]]:
    """Expand the matrix into a flat list of (program_id, region_id, prog_slug, region_name)
    tuples — one per future team. Length = 640. Shuffled so consecutive rows
    don't all land in the same cell when the writer iterates."""
    out: list[tuple[int, int, str, str]] = []
    for prog_slug, by_region in MATRIX.items():
        prog = programs[prog_slug]
        for region_name, count in by_region.items():
            region = regions[region_name]
            for _ in range(count):
                out.append((prog.id, region.id, prog_slug, region_name))
    random.Random(42).shuffle(out)   # deterministic shuffle for re-runs
    return out


def _gen_team_name(rng: random.Random, region_name: str, used_names: set[str]) -> str:
    """Build a "U14 - תל אביב" style name. Unique enough across the dataset
    that downstream pages don't render duplicate-looking rows; we tolerate
    duplicates if we exhaust the (age × place) Cartesian product."""
    places = PLACE_BY_REGION.get(region_name, ["—"])
    for _ in range(40):
        age = rng.choice(AGE_BRACKETS)
        place = rng.choice(places)
        candidate = f"{age} - {place}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
    # Fallback when the unique pool is exhausted — append a numeric suffix.
    seq = 2
    while True:
        candidate = f"{age} - {place} #{seq}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        seq += 1


async def _redistribute(s: AsyncSession, org_id: int, programs, regions, *, dry_run: bool) -> dict:
    assignments = await _flat_assignments(programs, regions)
    target_total = len(assignments)

    # Load all existing teams (ordered by id so we re-use stable rows first).
    existing = list(
        (await s.execute(
            select(TeamProfile).where(TeamProfile.organization_id == org_id).order_by(TeamProfile.id)
        )).scalars().all()
    )
    log.info(f"Existing teams: {len(existing)}; target: {target_total}")

    used_names: set[str] = {t.team_name for t in existing if t.team_name}

    updated = 0
    created = 0
    deleted = 0

    rng = random.Random(7)
    # 1) Re-assign as many existing teams as possible.
    n_reuse = min(len(existing), target_total)
    for i in range(n_reuse):
        team = existing[i]
        prog_id, region_id, _, _ = assignments[i]
        if team.region_id != region_id or team.program_id != prog_id:
            team.region_id = region_id
            team.program_id = prog_id
            updated += 1

    # 2) Create new rows for the deficit.
    if target_total > len(existing):
        for i in range(len(existing), target_total):
            prog_id, region_id, _, region_name = assignments[i]
            name = _gen_team_name(rng, region_name, used_names)
            new_team = TeamProfile(
                team_name=name,
                organization_id=org_id,
                region_id=region_id,
                program_id=prog_id,
            )
            s.add(new_team)
            created += 1

    # 3) (Defensive) — if surplus existing teams remain, leave them alone but
    #    null their program/region so they don't pollute the breakdown. We
    #    don't delete history: there may be players hanging off these rows.
    if len(existing) > target_total:
        for team in existing[target_total:]:
            team.region_id = None
            team.program_id = None
            updated += 1

    log.info(f"  updated: {updated}  created: {created}  cleared-surplus: {deleted}")

    if not dry_run:
        # Decouple regions from programs — they're shared geography now.
        await s.execute(update(Region).where(Region.organization_id == org_id).values(program_id=None))
        await s.commit()
        log.info("  region.program_id reset to NULL (regions are shared now)")
    else:
        await s.rollback()
        log.info("  DRY RUN — rolled back, no DB changes persisted")

    return {"updated": updated, "created": created, "target": target_total}


async def _verify(s: AsyncSession, org_id: int) -> None:
    log.info("")
    log.info("=== VERIFICATION ===")
    from sqlalchemy import func
    by_program = (await s.execute(
        select(Program.name, func.count(TeamProfile.id))
        .select_from(Program)
        .outerjoin(TeamProfile, (TeamProfile.program_id == Program.id) & (TeamProfile.organization_id == org_id))
        .where(Program.organization_id == org_id)
        .group_by(Program.id, Program.name)
        .order_by(func.count(TeamProfile.id).desc())
    )).all()
    log.info("Teams per program:")
    total = 0
    for name, c in by_program:
        log.info(f"  {name!r:30s} {c}")
        total += c
    log.info(f"  {'TOTAL':30s} {total}")

    by_region = (await s.execute(
        select(Region.name, func.count(TeamProfile.id))
        .select_from(Region)
        .outerjoin(TeamProfile, (TeamProfile.region_id == Region.id) & (TeamProfile.organization_id == org_id))
        .where(Region.organization_id == org_id)
        .group_by(Region.id, Region.name)
        .order_by(func.count(TeamProfile.id).desc())
    )).all()
    log.info("Teams per region:")
    for name, c in by_region:
        log.info(f"  {name!r:30s} {c}")


async def main(args: argparse.Namespace) -> int:
    _validate_matrix()
    async with AsyncSessionLocal() as s:
        org, programs, regions = await _load_or_die(s, args.org_slug)
        log.info(f"Org id={org.id} slug={org.slug!r}")
        await _redistribute(s, org.id, programs, regions, dry_run=args.dry_run)
    # Re-open a fresh session for verification so we read committed state.
    if not args.dry_run:
        async with AsyncSessionLocal() as s:
            await _verify(s, org.id)
    return 0


def _cli() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--org-slug", default=DEFAULT_ORG_SLUG)
    p.add_argument("--dry-run", action="store_true", help="Report plan without committing")
    return asyncio.run(main(p.parse_args()))


if __name__ == "__main__":
    sys.exit(_cli())
