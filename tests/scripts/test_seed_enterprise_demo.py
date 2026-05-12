"""Smoke test for scripts/seed_enterprise_demo.py.

Runs the seeder against the per-test in-memory SQLite with small counts
(scaled WAY down from the production 6,000-player default), then asserts
the expected row counts and that cleanup actually removes everything.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.branches import Branch
from src.models.organizations import Organization
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.regions import Region
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.models.users import User
from src.services.demo_seeder import (
    SeedConfig,
)
from src.services.demo_seeder import (
    cleanup_demo_tenant as _cleanup,
)
from src.services.demo_seeder import (
    seed_demo_tenant as _seed,
)

pytestmark = pytest.mark.asyncio


SMALL = SeedConfig(
    regions=2,
    branches_per_region=2,
    teams_per_branch=2,
    players_per_team=3,
    contacts_per_team=1,
)


async def _count(session: AsyncSession, model) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar() or 0)


async def test_seed_produces_expected_counts(db_session: AsyncSession):
    result = await _seed(db_session, "demo-test", "Demo Test Org", SMALL)

    # 2 regions × 2 branches = 4 branches
    # 4 branches × 2 teams = 8 teams
    # 8 teams × 3 players = 24 players
    # 8 teams × 1 contact = 8 contacts
    assert result["regions"] == 2
    assert result["branches"] == 4
    assert result["teams"] == 8
    assert result["players"] == 24
    assert result["contacts"] == 8

    # 1 org_admin + 2 region_managers + 4 branch_managers + 8 coaches = 15 users
    assert result["users"] == 15

    # Verify the actual DB row counts match.
    assert await _count(db_session, Organization) == 1
    assert await _count(db_session, Region) == 2
    assert await _count(db_session, Branch) == 4
    assert await _count(db_session, TeamProfile) == 8
    assert await _count(db_session, Player) == 24
    assert await _count(db_session, PlayerContact) == 8

    # 15 users + memberships (one per role assignment).
    assert await _count(db_session, User) == 15
    assert await _count(db_session, UserOrganization) == 15


async def test_seed_marks_org_with_seed_origin(db_session: AsyncSession):
    await _seed(db_session, "demo-marker", "Marker Test", SMALL)
    org = (
        await db_session.execute(
            select(Organization).where(Organization.slug == "demo-marker")
        )
    ).scalar_one()
    assert (org.attributes_json or {}).get("seed_origin") == "demo"
    assert (org.attributes_json or {}).get("seeded_at")


async def test_contacts_are_encrypted_at_rest(db_session: AsyncSession):
    """The PlayerContact rows go through EncryptedText so the raw cell is
    ciphertext. Spot-check one row."""
    from sqlalchemy import text

    await _seed(db_session, "demo-enc", "Demo Enc", SMALL)
    raw = (
        await db_session.execute(
            text(
                "SELECT parent_phone_enc, national_id_enc FROM player_contacts "
                "WHERE parent_phone_enc IS NOT NULL LIMIT 1"
            )
        )
    ).one()
    # Ciphertext is long and url-safe-base64-ish; not the literal phone string.
    assert raw[0] is not None and len(raw[0]) > 30
    assert not raw[0].startswith("050-")  # no plaintext leak


async def test_cleanup_removes_demo_tenant(db_session: AsyncSession):
    """After seed + cleanup the org + all child rows are gone, and the demo
    users (identified by email domain) are gone too."""
    await _seed(db_session, "demo-cleanup", "Demo Cleanup", SMALL)
    assert await _count(db_session, Organization) == 1
    assert await _count(db_session, User) == 15

    removed = await _cleanup(db_session, "demo-cleanup")
    assert removed is True

    assert await _count(db_session, Organization) == 0
    assert await _count(db_session, Region) == 0
    assert await _count(db_session, Branch) == 0
    assert await _count(db_session, TeamProfile) == 0
    assert await _count(db_session, Player) == 0
    assert await _count(db_session, PlayerContact) == 0
    assert await _count(db_session, UserOrganization) == 0
    # Users with @demo.nextplay.local emails are all gone.
    assert await _count(db_session, User) == 0


async def test_cleanup_refuses_non_demo_orgs(db_session: AsyncSession):
    """Cleanup must NEVER delete an org that wasn't seeded by us."""
    real_org = Organization(slug="real-customer", name="Real Customer")
    db_session.add(real_org)
    await db_session.flush()

    with pytest.raises(RuntimeError, match="seed_origin is not 'demo'"):
        await _cleanup(db_session, "real-customer")

    # Real org still exists.
    assert await _count(db_session, Organization) == 1


async def test_cleanup_noop_when_org_missing(db_session: AsyncSession):
    removed = await _cleanup(db_session, "ghost-slug")
    assert removed is False
