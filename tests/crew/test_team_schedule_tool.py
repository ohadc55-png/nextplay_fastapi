"""Phase 15.7 — `team_schedule` CrewAI tool.

Covers:
  - Factory produces a Tool with correct shape
  - JSON schema does NOT expose user_id / team_id
  - additionalProperties: false (blocks injection)
  - Handler returns coach's own events only — cross-tenant injection
    via the executor's stripped args still hits the closure-locked path
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import src.models  # noqa: F401
from src.core.database import Base
from src.crew.tools import (
    _AGENT_TOOL_MAP,
    execute_tool_call,
    make_team_schedule_tool,
)
from src.models.practice_sessions import PracticeSession
from src.models.teams import TeamProfile
from src.models.users import User

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


def test_schema_excludes_tenant_fields(db_session):
    tool = make_team_schedule_tool(db_session, user_id=1, team_id=10)
    schema = tool.parameters
    props = schema["properties"]
    assert "user_id" not in props
    assert "team_id" not in props
    assert schema["additionalProperties"] is False


def test_tool_registered_for_all_5_specialists():
    for agent in ("gm", "scout", "analytics", "tactics", "training"):
        assert "team_schedule" in _AGENT_TOOL_MAP[agent], agent


async def test_handler_returns_only_owners_events(db_session: AsyncSession):
    coach_a = User(email="a@x.com", password_hash="x", display_name="A")
    coach_b = User(email="b@x.com", password_hash="x", display_name="B")
    db_session.add_all([coach_a, coach_b])
    await db_session.flush()
    team_a = TeamProfile(team_name="A Team", user_id=coach_a.id)
    team_b = TeamProfile(team_name="B Team", user_id=coach_b.id)
    db_session.add_all([team_a, team_b])
    await db_session.flush()
    now = datetime.utcnow()
    db_session.add_all([
        PracticeSession(
            team_id=team_a.id, user_id=coach_a.id,
            title="A practice",
            scheduled_at=now + timedelta(days=2),
            duration_minutes=90,
            status="scheduled",
        ),
        PracticeSession(
            team_id=team_b.id, user_id=coach_b.id,
            title="B practice",
            scheduled_at=now + timedelta(days=2),
            duration_minutes=90,
            status="scheduled",
        ),
    ])
    await db_session.commit()

    tool = make_team_schedule_tool(db_session, user_id=coach_a.id, team_id=team_a.id)
    result = await tool.handler(days_ahead=7)
    assert result["count"] == 1
    assert result["events"][0]["title"] == "A practice"


async def test_handler_rejects_injected_tenant_fields(db_session: AsyncSession):
    """Executor strips user_id/team_id from the args dict, but even if it
    didn't, the handler signature ignores them — closure values win.
    """
    coach = User(email="c@x.com", password_hash="x", display_name="C")
    db_session.add(coach)
    await db_session.flush()
    team = TeamProfile(team_name="C Team", user_id=coach.id)
    db_session.add(team)
    await db_session.flush()
    await db_session.commit()

    tool = make_team_schedule_tool(db_session, user_id=coach.id, team_id=team.id)
    result = await execute_tool_call(
        [tool], "team_schedule",
        {"user_id": 99999, "team_id": 99999, "days_ahead": 7},  # injection
    )
    # Doesn't error — injection silently dropped, closure values used.
    assert "events" in result
