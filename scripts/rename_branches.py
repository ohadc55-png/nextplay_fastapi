"""Rename existing branches to match their region (non-destructive).

The demo seeder used to pick branch names from a flat city list, with no
guarantee that the city geographically belongs to its region (e.g.,
"סניף תל אביב" could end up under "מחוז צפון"). This script walks every
branch of the target org, looks at its `region_id`, and renames it to a
value from `HEBREW_BRANCHES_BY_REGION` for that region.

- Idempotent — running twice produces the same names.
- Non-destructive — only the `branches.name` column is touched.
  All teams, players, contacts, memberships, audit rows stay intact.

CLI:
    python scripts/rename_branches.py                    # default slug
    python scripts/rename_branches.py --slug=foo
    python scripts/rename_branches.py --dry-run          # preview only
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make `src` importable when run as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src.core.database import AsyncSessionLocal  # noqa: E402
from src.models.branches import Branch  # noqa: E402
from src.models.organizations import Organization  # noqa: E402
from src.models.regions import Region  # noqa: E402
from src.services.demo_seeder import HEBREW_BRANCHES_BY_REGION  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("rename")


async def rename_org_branches(slug: str, *, dry_run: bool) -> dict:
    async with AsyncSessionLocal() as session:
        org = (
            await session.execute(
                select(Organization).where(Organization.slug == slug)
            )
        ).scalar_one_or_none()
        if org is None:
            log.error(f"Org with slug={slug!r} not found.")
            return {"error": "not_found"}

        regions = list(
            (
                await session.execute(
                    select(Region).where(Region.organization_id == org.id)
                )
            )
            .scalars()
            .all()
        )
        if not regions:
            log.warning(f"Org {slug!r} has no regions; nothing to do.")
            return {"renamed": 0}

        total_renamed = 0
        for region in regions:
            curated = HEBREW_BRANCHES_BY_REGION.get(region.name)
            if not curated:
                log.warning(
                    f"  region {region.name!r} has no curated branch names — skip"
                )
                continue

            # Deterministic order: by branch id. This matches the seeder's
            # insertion order so the first branch ever created in this region
            # gets the first curated name.
            branches = list(
                (
                    await session.execute(
                        select(Branch)
                        .where(Branch.region_id == region.id)
                        .order_by(Branch.id)
                    )
                )
                .scalars()
                .all()
            )
            log.info(f"region {region.name}: {len(branches)} branches")
            for i, branch in enumerate(branches):
                if i < len(curated):
                    new_name = curated[i]
                else:
                    new_name = f"{region.name} {i + 1}"
                if branch.name != new_name:
                    log.info(f"  {branch.name!r:<32} -> {new_name!r}")
                    if not dry_run:
                        branch.name = new_name
                    total_renamed += 1

        if dry_run:
            log.info(f"DRY RUN — would have renamed {total_renamed} branches.")
            return {"renamed": total_renamed, "dry_run": True}

        await session.commit()
        log.info(f"DONE — renamed {total_renamed} branches.")
        return {"renamed": total_renamed}


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--slug", default="shaar-shivyon", help="Org slug")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing")
    return ap


if __name__ == "__main__":
    args = _build_parser().parse_args()
    result = asyncio.run(rename_org_branches(args.slug, dry_run=args.dry_run))
    sys.exit(0 if "error" not in result else 1)
