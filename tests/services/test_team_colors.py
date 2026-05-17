"""Phase 15.1 — team_colors palette helper.

Covers:
  - Determinism: same team_id always maps to the same color.
  - Palette size + uniqueness in the first 8 teams (no early collision).
  - Modulo rollover for team_id > 8.
  - `ensure_team_color` writes back only on missing color_hex.
  - Defensive fallback on malformed team_id.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import src.models  # noqa: F401 — register ORM classes
from src.core.database import Base
from src.models.teams import TeamProfile
from src.services.team_colors import PALETTE, ensure_team_color, palette_for_team

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def test_palette_has_8_distinct_colors():
    assert len(PALETTE) == 8
    assert len(set(PALETTE)) == 8


def test_palette_for_team_is_deterministic():
    """Calling twice on the same id returns the same hex."""
    for team_id in (1, 7, 99, 12345):
        assert palette_for_team(team_id) == palette_for_team(team_id)


def test_palette_for_team_first_eight_are_unique():
    """First 8 teams should each get a distinct color (no collision until 9)."""
    colors = {palette_for_team(i) for i in range(1, 9)}
    assert len(colors) == 8


def test_palette_for_team_rolls_over_after_8():
    """Team 9 collides with team 1 (palette repeats every 8)."""
    assert palette_for_team(1) == palette_for_team(9)
    assert palette_for_team(2) == palette_for_team(10)


def test_palette_for_team_handles_bad_inputs():
    """Non-int / negative / zero falls back to PALETTE[0] without raising."""
    assert palette_for_team(0) == PALETTE[0]
    assert palette_for_team(-1) == PALETTE[0]


async def test_ensure_team_color_writes_when_missing(db_session: AsyncSession):
    team = TeamProfile(team_name="Test Team")
    db_session.add(team)
    await db_session.flush()
    assert team.color_hex is None

    color = await ensure_team_color(db_session, team)
    assert color == palette_for_team(team.id)
    assert team.color_hex == color


async def test_ensure_team_color_is_idempotent(db_session: AsyncSession):
    """A team already carrying a custom color is NOT overwritten."""
    team = TeamProfile(team_name="Custom", color_hex="#abc123")
    db_session.add(team)
    await db_session.flush()

    color = await ensure_team_color(db_session, team)
    assert color == "#abc123"
    assert team.color_hex == "#abc123"  # untouched
