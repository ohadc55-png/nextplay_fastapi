"""Seed a demo Sha'ar Shivyon org account: 3 users + 1 region + 1 team + 20 players.

Idempotent — re-running produces the same final state. Use to spin up a
demoable slice of the Enterprise/Coach Calendar flow on staging or production.

The org slug is `shaar-shivyon-demo` (NOT plain `shaar-shivyon`) so the
demo data never collides with a real customer org. The Phase 13 slug URLs
expose this org at `trynextplay.app/shaar-shivyon-demo/dashboard`.

What gets created:
  * Organization slug=`shaar-shivyon-demo` (existing one is reused if present).
  * 6 Sha'ar Shivyon programs (verbatim from `seed_shaar_shivyon_programs.py`).
  * Region "מחוז מרכז" (Center) with `program_id=NULL` (Phase 12 shared).
  * Team "תל אביב דרום" in (region=Center, program=Sal-Tech), `user_id=NULL`
    so the user can invite a real coach via the Phase 14 UI on production.
  * 3 demo users + 3 `UserOrganization` rows:
      ceo@shaar-shivyon-demo.demo.nextplay.local           → org_admin
      pm-saltech@shaar-shivyon-demo.demo.nextplay.local    → program_manager (sal-tech)
      rm-merkaz@shaar-shivyon-demo.demo.nextplay.local     → region_manager (Center)
    All passwords: `demo123` (matches `src/services/demo_seeder.py` convention).
  * 20 Player rows on the team with realistic mixed Israeli names.

Usage (PowerShell):

    # Local SQLite (default)
    python scripts/seed_shaar_shivyon_demo_account.py

    # Railway staging / production
    $env:DATABASE_URL = "postgresql://...railway.app:5432/railway"
    python scripts/seed_shaar_shivyon_demo_account.py

    # See what would be created without writing
    python scripts/seed_shaar_shivyon_demo_account.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.password_service import hash_password
from src.core.database import AsyncSessionLocal
from src.models.organizations import Organization
from src.models.players import Player
from src.models.programs import Program
from src.models.regions import Region
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.models.users import User

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed_demo")

# ---------------------------------------------------------------------------
# Constants — mirror seed_shaar_shivyon_programs.py to keep the org + program
# set identical across both scripts. A drift here would mean re-running both
# scripts on the same DB produces different results depending on order.
# ---------------------------------------------------------------------------

DEFAULT_ORG_SLUG = "shaar-shivyon-demo"
DEFAULT_ORG_NAME = "עמותת שער שיוויון (דמו)"
DEFAULT_REGION_NAME = "מחוז מרכז"
DEFAULT_TEAM_NAME = "תל אביב דרום"
DEFAULT_PROGRAM_SLUG = "sal-tech"

SHA_AR_SHIVYON_PROGRAMS: list[tuple[str, str]] = [
    ("שער שיוויון", "shaar-shivyon"),
    ("בועטות", "boatot"),
    ("סל-טק", "sal-tech"),
    ("מלכת הסלים", "malkat-ha-salim"),
    ("שווים לניצחון", "shavim-le-nitzachon"),
    ("חותרים להצלחה", "hotrim-le-hatzlacha"),
]

# Demo account convention from src/services/demo_seeder.py:46-47.
# Hash once and reuse — bcrypt is ~250ms per call.
DEMO_PASSWORD = "demo123"
DEMO_EMAIL_DOMAIN = "shaar-shivyon-demo.demo.nextplay.local"

# (email-local-part, display-name, role-tag-for-scoping)
DEMO_USERS: list[tuple[str, str, str]] = [
    ("ceo",        "מנכ\"ל שער שיוויון (דמו)",         "org_admin"),
    ("pm-saltech", "מנהל תוכנית סל-טק (דמו)",          "program_manager"),
    ("rm-merkaz",  "מנהל מחוז מרכז (דמו)",             "region_manager"),
]

# 20 player names + jersey numbers + ages. Realistic mixed Israeli names —
# spread across boys/girls common to youth basketball rosters. Numbers
# chosen non-sequentially to feel real (no "1, 2, 3, ..."). Ages 12-16
# matches a typical U14-U16 squad.
DEMO_PLAYERS: list[tuple[str, int, int, str]] = [
    # (name, jersey_number, age, position)
    ("דני כהן",         4,  13, "Point Guard"),
    ("איתי לוי",        5,  14, "Shooting Guard"),
    ("יונתן אברהמי",    7,  14, "Small Forward"),
    ("נועם פרץ",        8,  13, "Point Guard"),
    ("עומר מזרחי",      9,  15, "Power Forward"),
    ("איתן בן-דוד",    10,  14, "Center"),
    ("שחר רוזנברג",    11,  15, "Shooting Guard"),
    ("ליאון אדרי",     12,  13, "Small Forward"),
    ("דניאל סבן",      13,  14, "Point Guard"),
    ("רועי שפירא",     14,  15, "Power Forward"),
    ("עידן חזן",       15,  14, "Center"),
    ("אורי כץ",        16,  13, "Shooting Guard"),
    ("יואב גבאי",      17,  16, "Small Forward"),
    ("מתן סולומון",    18,  14, "Power Forward"),
    ("עידו פרידמן",    19,  15, "Point Guard"),
    ("גיא אטיאס",      21,  13, "Shooting Guard"),
    ("הראל אזולאי",    22,  16, "Center"),
    ("טל ביטון",       23,  14, "Small Forward"),
    ("עידן שמש",       24,  15, "Power Forward"),
    ("ניב לוין",       25,  12, "Point Guard"),
]


# ---------------------------------------------------------------------------
# Idempotent helpers — each one returns the existing row if found, creates
# otherwise. Lookups are keyed on the natural-unique attribute for the
# entity so reruns produce the same final state.
# ---------------------------------------------------------------------------


async def _get_or_create_org(
    session: AsyncSession, *, slug: str, name: str
) -> Organization:
    row = (
        await session.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()
    if row is not None:
        log.info(f"  [org] existing id={row.id} slug={row.slug!r}")
        return row

    # In-place rename: if a prior run of this seed used the old slug
    # `shaar-shivyon` (collides with the real Sha'ar Shivyon org concept),
    # migrate it to the new slug + bring user emails along. One-shot, then
    # the existing-row branch above takes over on subsequent runs.
    if slug == "shaar-shivyon-demo":
        legacy = (
            await session.execute(
                select(Organization).where(Organization.slug == "shaar-shivyon")
            )
        ).scalar_one_or_none()
        if legacy is not None:
            legacy.slug = slug
            if legacy.name == "עמותת שער שיוויון":
                legacy.name = name
            # Rename user emails: @shaar-shivyon.demo.nextplay.local
            # -> @shaar-shivyon-demo.demo.nextplay.local
            old_dom = "shaar-shivyon.demo.nextplay.local"
            new_dom = "shaar-shivyon-demo.demo.nextplay.local"
            users = (
                await session.execute(
                    select(User).where(User.email.like(f"%@{old_dom}"))
                )
            ).scalars().all()
            for u in users:
                local = u.email.split("@", 1)[0]
                u.email = f"{local}@{new_dom}"
            await session.flush()
            log.info(
                f"  [org] renamed legacy id={legacy.id} slug='shaar-shivyon' -> "
                f"{slug!r}; updated {len(users)} user emails to @{new_dom}"
            )
            return legacy

    org = Organization(slug=slug, name=name, status="active", plan="enterprise")
    session.add(org)
    await session.flush()
    log.info(f"  [org] created id={org.id} slug={org.slug!r}")
    return org


async def _ensure_program(
    session: AsyncSession, *, org_id: int, name: str, slug: str
) -> Program:
    existing = (
        await session.execute(
            select(Program).where(
                Program.organization_id == org_id, Program.name == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.slug != slug:
            existing.slug = slug
        return existing
    program = Program(organization_id=org_id, name=name, slug=slug)
    session.add(program)
    await session.flush()
    log.info(f"    [program] created id={program.id} name={name!r}")
    return program


async def _ensure_all_programs(
    session: AsyncSession, *, org_id: int
) -> dict[str, Program]:
    """Returns slug → Program for all 6 Sha'ar Shivyon programs."""
    out: dict[str, Program] = {}
    for name, slug in SHA_AR_SHIVYON_PROGRAMS:
        out[slug] = await _ensure_program(
            session, org_id=org_id, name=name, slug=slug,
        )
    return out


async def _get_or_create_region(
    session: AsyncSession, *, org_id: int, name: str
) -> Region:
    """Region with program_id=NULL — Phase 12 shared geography. Uniqueness
    enforced by `uq_regions_org_name` (org_id, name) so a re-run never
    inserts a duplicate."""
    existing = (
        await session.execute(
            select(Region).where(
                Region.organization_id == org_id, Region.name == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        log.info(f"  [region] existing id={existing.id} name={name!r}")
        return existing
    region = Region(organization_id=org_id, name=name, program_id=None)
    session.add(region)
    await session.flush()
    log.info(f"  [region] created id={region.id} name={name!r}")
    return region


async def _get_or_create_team(
    session: AsyncSession,
    *,
    org_id: int,
    region_id: int,
    program_id: int,
    team_name: str,
) -> TeamProfile:
    """Team is org-owned (`user_id=NULL`) — Phase 14's invite-with-team-id
    flow populates `user_id` when a real coach redeems the invite."""
    existing = (
        await session.execute(
            select(TeamProfile).where(
                TeamProfile.organization_id == org_id,
                TeamProfile.team_name == team_name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Make sure region/program point at the right rows even if the team
        # row predates this script. Cheap idempotent fix-up.
        changed = False
        if existing.region_id != region_id:
            existing.region_id = region_id
            changed = True
        if existing.program_id != program_id:
            existing.program_id = program_id
            changed = True
        suffix = " (fixed up region/program)" if changed else ""
        log.info(f"  [team] existing id={existing.id} name={team_name!r}{suffix}")
        return existing
    team = TeamProfile(
        team_name=team_name,
        organization_id=org_id,
        region_id=region_id,
        program_id=program_id,
        user_id=None,  # awaiting coach via Phase 14 invite
    )
    session.add(team)
    await session.flush()
    log.info(f"  [team] created id={team.id} name={team_name!r}")
    return team


async def _seed_demo_users(
    session: AsyncSession,
    *,
    org: Organization,
    sal_tech: Program,
    region: Region,
) -> dict[str, User]:
    """Creates the 3 demo users + their `UserOrganization` rows. Returns the
    user objects keyed by their role tag for log clarity. Password is hashed
    once and reused — bcrypt is ~250ms per call."""
    pwd_hash = hash_password(DEMO_PASSWORD)
    out: dict[str, User] = {}

    for local, display, role in DEMO_USERS:
        email = f"{local}@{DEMO_EMAIL_DOMAIN}"
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                email=email,
                password_hash=pwd_hash,
                display_name=display,
                role="coach",  # base User.role is informational; org role
                                # lives on UserOrganization
                is_active=1,
                email_verified=1,
                subscription_plan="club",  # enterprise users skip the trial gate
            )
            session.add(user)
            await session.flush()
            log.info(f"  [user] created id={user.id} email={email!r} ({role})")
        else:
            log.info(f"  [user] existing id={user.id} email={email!r} ({role})")
        out[role] = user

        # Membership row — uniqueness on (user_id, organization_id, role)
        # makes the upsert idempotent.
        membership = (
            await session.execute(
                select(UserOrganization).where(
                    UserOrganization.user_id == user.id,
                    UserOrganization.organization_id == org.id,
                    UserOrganization.role == role,
                )
            )
        ).scalar_one_or_none()
        program_scope: int | None = None
        region_scope: int | None = None
        if role == "program_manager":
            program_scope = sal_tech.id
        elif role == "region_manager":
            region_scope = region.id

        if membership is None:
            session.add(
                UserOrganization(
                    user_id=user.id,
                    organization_id=org.id,
                    role=role,
                    program_id=program_scope,
                    region_id=region_scope,
                    status="active",
                )
            )
            await session.flush()
            log.info(
                f"    [membership] role={role} program_id={program_scope} "
                f"region_id={region_scope}"
            )
        else:
            # Fix up scope if it drifted.
            if membership.program_id != program_scope:
                membership.program_id = program_scope
            if membership.region_id != region_scope:
                membership.region_id = region_scope
    return out


async def _seed_players(
    session: AsyncSession, *, org: Organization, team: TeamProfile,
) -> int:
    """Insert/refresh 20 Player rows on the team. Uniqueness is by
    (organization_id, team_id, name) — a non-DB constraint, just our seed
    discipline. Returns the count of rows that now exist on the team."""
    created = 0
    for name, number, age, position in DEMO_PLAYERS:
        existing = (
            await session.execute(
                select(Player).where(
                    Player.organization_id == org.id,
                    Player.team_id == team.id,
                    Player.name == name,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        player = Player(
            organization_id=org.id,
            team_id=team.id,
            user_id=None,  # no coach owner yet — Phase 14 invite populates
            name=name,
            number=number,
            age=age,
            position=position,
            active=True,
        )
        session.add(player)
        created += 1
    if created:
        await session.flush()
    log.info(f"  [players] {created} created, {len(DEMO_PLAYERS) - created} already present")
    return created


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        log.info(f"=== Sha'ar Shivyon demo seed (org={args.org_slug!r}) ===")

        org = await _get_or_create_org(
            session, slug=args.org_slug, name=args.org_name,
        )
        programs = await _ensure_all_programs(session, org_id=org.id)
        sal_tech = programs[DEFAULT_PROGRAM_SLUG]
        region = await _get_or_create_region(
            session, org_id=org.id, name=args.region_name,
        )
        team = await _get_or_create_team(
            session,
            org_id=org.id,
            region_id=region.id,
            program_id=sal_tech.id,
            team_name=args.team_name,
        )
        await _seed_demo_users(session, org=org, sal_tech=sal_tech, region=region)
        await _seed_players(session, org=org, team=team)

        if args.dry_run:
            log.info("--- DRY RUN — rolling back ---")
            await session.rollback()
        else:
            await session.commit()
            log.info("--- committed ---")

    log.info("")
    log.info("Demo accounts (all password: demo123):")
    for local, _display, role in DEMO_USERS:
        log.info(f"  {local}@{DEMO_EMAIL_DOMAIN}  ({role})")
    return 0


def _cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org-slug", default=DEFAULT_ORG_SLUG)
    parser.add_argument("--org-name", default=DEFAULT_ORG_NAME)
    parser.add_argument("--region-name", default=DEFAULT_REGION_NAME)
    parser.add_argument("--team-name", default=DEFAULT_TEAM_NAME)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be created, then roll back without committing.",
    )
    args = parser.parse_args()
    return asyncio.run(main(args))


if __name__ == "__main__":
    sys.exit(_cli())
