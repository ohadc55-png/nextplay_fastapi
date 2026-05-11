"""CLI wrapper for the demo tenant seeder.

The actual seeding logic lives in `src/services/demo_seeder.py` so it can
be imported from tests + future admin endpoints without path-mangling.

CLI:
    python scripts/seed_enterprise_demo.py                       # varied default (multi-coach, per-region counts)
    python scripts/seed_enterprise_demo.py --reset               # wipe + reseed
    python scripts/seed_enterprise_demo.py --cleanup             # wipe only
    python scripts/seed_enterprise_demo.py --uniform             # legacy uniform 6×20×5×10 shape
    python scripts/seed_enterprise_demo.py --regions 3 --branches-per-region 5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow `python scripts/seed_enterprise_demo.py` to find src.* without
# requiring `python -m`. (Tests don't hit this — they import directly from
# src.services.demo_seeder.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src.core.database import AsyncSessionLocal  # noqa: E402
from src.models.organizations import Organization  # noqa: E402
from src.services.demo_seeder import (  # noqa: E402
    DEFAULT_NAME,
    DEFAULT_SLUG,
    DEMO_EMAIL_DOMAIN,
    DEMO_PASSWORD,
    REGION_CONFIGS_DEFAULT,
    SeedConfig,
    cleanup_demo_tenant,
    seed_demo_tenant,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed")


async def main(args: argparse.Namespace) -> int:
    # Detect whether the user passed any per-uniform-bucket override flags.
    # If so, fall back to the legacy uniform shape. Otherwise default to the
    # varied per-region distribution + multi-team coaches.
    uniform_overrides = any(v is not None for v in (
        args.regions, args.branches_per_region, args.teams_per_branch,
        args.players_per_team,
    ))
    if args.uniform or uniform_overrides:
        cfg = SeedConfig(
            regions=args.regions if args.regions is not None else 6,
            branches_per_region=args.branches_per_region if args.branches_per_region is not None else 20,
            teams_per_branch=args.teams_per_branch if args.teams_per_branch is not None else 5,
            players_per_team=args.players_per_team if args.players_per_team is not None else 10,
            contacts_per_team=args.contacts_per_team,
        )
    else:
        cfg = SeedConfig(
            region_configs=REGION_CONFIGS_DEFAULT,
            multi_team_coach=not args.single_team_coach,
            players_per_team_range=(8, 14),
            contacts_per_team=args.contacts_per_team,
        )

    async with AsyncSessionLocal() as session:
        if args.cleanup:
            log.info(f"Cleaning up demo org slug={args.slug!r}…")
            await cleanup_demo_tenant(session, args.slug)
            return 0

        if args.reset:
            log.info(f"Resetting demo org slug={args.slug!r}…")
            await cleanup_demo_tenant(session, args.slug)

        existing = (
            await session.execute(
                select(Organization).where(Organization.slug == args.slug)
            )
        ).scalar_one_or_none()
        if existing is not None:
            log.error(
                f"Org with slug={args.slug!r} already exists (id={existing.id}). "
                f"Re-run with --reset to wipe and reseed, or --cleanup to wipe only."
            )
            return 2

        result = await seed_demo_tenant(session, args.slug, args.name, cfg)

    log.info("")
    log.info("=" * 60)
    log.info(f"DONE in {result['elapsed_s']}s — org id={result['org_id']}")
    log.info(f"  regions      = {result['regions']}")
    log.info(f"  branches     = {result['branches']}")
    log.info(f"  teams        = {result['teams']}")
    log.info(f"  players      = {result['players']}")
    log.info(f"  contacts     = {result['contacts']} (encrypted at rest)")
    log.info(f"  users        = {result['users']}")
    log.info("")
    log.info(f"Login as the demo Org Admin:")
    log.info(f"  email    = admin@{DEMO_EMAIL_DOMAIN}")
    log.info(f"  password = {DEMO_PASSWORD}")
    log.info(f"  url      = http://127.0.0.1:5050/org/login")
    log.info("=" * 60)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--slug", default=DEFAULT_SLUG)
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--reset", action="store_true", help="Wipe + reseed")
    ap.add_argument("--cleanup", action="store_true", help="Wipe only")
    ap.add_argument("--regions", type=int, default=None,
                    help="(uniform mode) number of regions")
    ap.add_argument("--branches-per-region", type=int, default=None,
                    help="(uniform mode) branches per region")
    ap.add_argument("--teams-per-branch", type=int, default=None,
                    help="(uniform mode) teams per branch")
    ap.add_argument("--players-per-team", type=int, default=None,
                    help="(uniform mode) players per team")
    ap.add_argument("--contacts-per-team", type=int, default=3)
    ap.add_argument("--uniform", action="store_true",
                    help="Force the legacy uniform 6×20×5×10 shape")
    ap.add_argument("--single-team-coach", action="store_true",
                    help="Disable multi-team coaches (varied mode only)")
    return ap


if __name__ == "__main__":
    args = _build_parser().parse_args()
    sys.exit(asyncio.run(main(args)))
