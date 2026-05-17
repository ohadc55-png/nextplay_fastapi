"""Seed the 6 Sha'ar Shivyon programs under the active org.

Creates (or finds) an Organization with slug='shaar-shivyon' and ensures
the 6 programs exist on it. Idempotent — safe to re-run.

Usage (PowerShell):

    # Local SQLite (default — leave DATABASE_URL empty)
    python scripts/seed_shaar_shivyon_programs.py

    # Railway production
    $env:DATABASE_URL = "postgresql://...railway.app:5432/railway"
    python scripts/seed_shaar_shivyon_programs.py

    # Custom org slug (e.g. another org you want to attach these programs to)
    python scripts/seed_shaar_shivyon_programs.py --org-slug my-org-slug
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from src.core.database import AsyncSessionLocal
from src.models.organizations import Organization
from src.models.programs import Program

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed_programs")

# Order matters: shown in this order in the UI. Slugs are kebab-cased
# transliterations — the front-end uses `name` for display and falls back
# to slug for URLs.
SHA_AR_SHIVYON_PROGRAMS: list[tuple[str, str]] = [
    ("שער שיוויון", "shaar-shivyon"),
    ("בועטות", "boatot"),
    ("סל-טק", "sal-tech"),
    ("מלכת הסלים", "malkat-ha-salim"),
    ("שווים לניצחון", "shavim-le-nitzachon"),
    ("חותרים להצלחה", "hotrim-le-hatzlacha"),
]

DEFAULT_ORG_SLUG = "shaar-shivyon"
DEFAULT_ORG_NAME = "עמותת שער שיוויון"


async def _get_or_create_org(session, *, slug: str, name: str) -> Organization:
    row = (
        await session.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()
    if row is not None:
        log.info(f"  Found existing org id={row.id} slug={row.slug!r}")
        return row
    org = Organization(slug=slug, name=name, status="active", plan="enterprise")
    session.add(org)
    await session.flush()
    log.info(f"  Created org id={org.id} slug={org.slug!r}")
    return org


async def _ensure_program(session, *, org_id: int, name: str, slug: str) -> Program:
    existing = (
        await session.execute(
            select(Program).where(
                Program.organization_id == org_id,
                Program.name == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Refresh slug in case the seed list was updated.
        if existing.slug != slug:
            existing.slug = slug
            log.info(f"    Updated slug for program {name!r} -> {slug}")
        return existing
    program = Program(organization_id=org_id, name=name, slug=slug)
    session.add(program)
    await session.flush()
    log.info(f"    Created program id={program.id} name={name!r} slug={slug}")
    return program


async def main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        log.info(f"Seeding programs for org slug={args.org_slug!r}…")
        org = await _get_or_create_org(
            session, slug=args.org_slug, name=args.org_name
        )

        for name, slug in SHA_AR_SHIVYON_PROGRAMS:
            await _ensure_program(session, org_id=org.id, name=name, slug=slug)

        await session.commit()

    log.info("Done.")
    return 0


def _cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org-slug", default=DEFAULT_ORG_SLUG)
    parser.add_argument("--org-name", default=DEFAULT_ORG_NAME)
    args = parser.parse_args()
    return asyncio.run(main(args))


if __name__ == "__main__":
    sys.exit(_cli())
