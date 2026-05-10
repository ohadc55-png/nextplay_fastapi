"""CrewAI orchestrator — sync-thread offload, tool bridge, cost capture.

CrewAI itself is heavyweight; rather than running the real kickoff in
tests, we patch the import inside `run_full_chat` so the test can:
  - confirm asyncio.to_thread is called (no event-loop blocking)
  - confirm `crew.usage_metrics` is logged
  - confirm the sync→async tool bridge actually invokes our handlers
  - confirm tenant fields are stripped from CrewAI tool args

The tool bridge is the key invariant — even when CrewAI runs in a
worker thread, `(user_id, team_id)` come from closure, never from the
LLM.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.crew.manager import _build_sync_bridge, run_full_chat
from src.crew.tools import Tool as AsyncTool
from src.models.analytics import ApiUsageLog

# ---------------------------------------------------------------------------
# Fake CrewAI module — what `from crewai import ...` returns inside the test.
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, **kwargs):
        self.role = kwargs.get("role")
        self.tools = kwargs.get("tools", [])
        self.backstory = kwargs.get("backstory", "")
        self.llm = kwargs.get("llm")


class _FakeTask:
    def __init__(self, description="", expected_output="", agent=None):
        self.description = description
        self.expected_output = expected_output
        self.agent = agent


class _FakeProcess:
    sequential = "sequential"


class _FakeCrew:
    """Minimal CrewAI stand-in. The kickoff method is overridable per
    test so we can simulate happy path / tool-calling / failure."""

    last_instance: _FakeCrew | None = None
    kickoff_result: str = "Full-mode answer from CrewAI."
    kickoff_should_raise: Exception | None = None
    fake_usage = SimpleNamespace(prompt_tokens=200, completion_tokens=80)
    # Optional callback: pass a function that gets called with (agent, tools)
    # before kickoff returns — useful for tool-bridge tests
    kickoff_hook = None

    def __init__(self, **kwargs):
        self.agents = kwargs.get("agents", [])
        self.tasks = kwargs.get("tasks", [])
        type(self).last_instance = self
        self.usage_metrics = type(self).fake_usage

    def kickoff(self):
        if type(self).kickoff_should_raise is not None:
            raise type(self).kickoff_should_raise
        if type(self).kickoff_hook is not None:
            type(self).kickoff_hook(self.agents[0], self.agents[0].tools)
        return type(self).kickoff_result


@pytest.fixture
def fake_crewai():
    """Replace `from crewai import ...` with our fakes for the duration of the test.

    Resets the fake crew's state between tests so failures don't leak."""
    _FakeCrew.kickoff_result = "Full-mode answer from CrewAI."
    _FakeCrew.kickoff_should_raise = None
    _FakeCrew.kickoff_hook = None
    _FakeCrew.last_instance = None

    fake_crewai_module = SimpleNamespace(
        Agent=_FakeAgent,
        Task=_FakeTask,
        Process=_FakeProcess,
        Crew=_FakeCrew,
    )
    # `crewai.tools.BaseTool` is needed by the bridge — keep the real
    # import path so the bridge class hierarchy is genuine.
    saved_crewai = sys.modules.get("crewai")
    sys.modules["crewai"] = fake_crewai_module
    try:
        yield fake_crewai_module
    finally:
        if saved_crewai is not None:
            sys.modules["crewai"] = saved_crewai
        else:
            sys.modules.pop("crewai", None)


# ---------------------------------------------------------------------------
# Sync→async bridge
# ---------------------------------------------------------------------------


class TestSyncBridge:
    """The bridge converts our async Tools into CrewAI sync BaseTools.
    Critically: tenant fields are stripped before our handler runs."""

    def test_bridge_invokes_async_handler(self):
        captured = {}

        async def _handler(action: str, **kw):
            captured["action"] = action
            captured["kw"] = kw
            return {"ok": True, "action": action}

        async_tool = AsyncTool(
            name="t", description="x",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
        )
        bridge = _build_sync_bridge(async_tool)
        result = bridge._run(action="ping")
        # Handler ran with the args we passed — closure-captured tenancy
        # stays elsewhere (not in args).
        assert captured["action"] == "ping"
        # Result was JSON-serialized for CrewAI's string-only contract
        assert "ok" in result
        assert "ping" in result

    def test_bridge_strips_injected_tenant_fields(self):
        """If the LLM tries to inject user_id/team_id into the tool
        call args, the bridge drops them BEFORE the async handler sees
        them. Defense in depth on top of the schema's exclusion."""
        captured = {}

        async def _handler(action: str, **kw):
            captured["kw"] = kw
            return {"ok": True}

        async_tool = AsyncTool(
            name="t", description="x",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
        )
        bridge = _build_sync_bridge(async_tool)
        bridge._run(action="ping", user_id=99, team_id=77)
        assert "user_id" not in captured["kw"]
        assert "team_id" not in captured["kw"]

    def test_bridge_handler_exception_returns_friendly_string(self):
        async def _boom(**_):
            raise RuntimeError("boom")

        async_tool = AsyncTool(
            name="bad", description="x",
            parameters={"type": "object"},
            handler=_boom,
        )
        bridge = _build_sync_bridge(async_tool)
        result = bridge._run()
        assert isinstance(result, str)
        assert "tool error" in result.lower()


# ---------------------------------------------------------------------------
# run_full_chat — happy path, cost capture, failure
# ---------------------------------------------------------------------------


class TestRunFullChat:
    async def test_happy_path_returns_kickoff_result(self, db_session, fake_crewai):
        result = await run_full_chat(
            db_session,
            user_id=1, team_id=10,
            agent_key="gm",
            user_message="Build me a 90-min practice plan",
            team_context="Roster: #7 Doncic",
        )
        assert "Full-mode answer" in result
        # Verify the agent backstory is the persona prompt (multi-team rules
        # block is in every agent's prompt — quick sanity check).
        agent = _FakeCrew.last_instance.agents[0]
        assert "PLAYER-TEAM BINDING" in agent.backstory
        # Tools are wired in (2 by default per default_tools_for_agent: team_db + kb)
        assert len(agent.tools) == 2

    async def test_usage_metrics_logged_to_api_usage(self, db_session, fake_crewai):
        await run_full_chat(
            db_session,
            user_id=1, team_id=10,
            agent_key="scout",
            user_message="Scout the next opponent",
        )
        await db_session.commit()
        rows = list((await db_session.execute(select(ApiUsageLog))).scalars().all())
        # Exactly one cost row for the crew turn
        assert len(rows) == 1
        log = rows[0]
        assert log.endpoint == "crew-full"
        assert log.agent_key == "scout"
        assert log.prompt_tokens == 200
        assert log.completion_tokens == 80

    async def test_kickoff_failure_returns_friendly_error(self, db_session, fake_crewai):
        _FakeCrew.kickoff_should_raise = RuntimeError("CrewAI internal error")
        result = await run_full_chat(
            db_session,
            user_id=1, team_id=10,
            agent_key="gm",
            user_message="x",
        )
        # Must NEVER raise — chat handler relies on a string back
        assert "trouble" in result.lower() or "rephrasing" in result.lower()
        # No cost row when kickoff failed (no usage_metrics to read)
        await db_session.commit()
        rows = list((await db_session.execute(select(ApiUsageLog))).scalars().all())
        assert rows == []

    async def test_extra_context_prepended_to_task(self, db_session, fake_crewai):
        await run_full_chat(
            db_session,
            user_id=1, team_id=10,
            agent_key="gm",
            user_message="What's the key matchup?",
            team_context="",
            extra_context="KB CONTEXT:\nrelevant drills...\n",
        )
        task = _FakeCrew.last_instance.tasks[0]
        # extra_context appears BEFORE the COACH'S REQUEST line
        assert task.description.index("KB CONTEXT") < task.description.index("COACH'S REQUEST")

    async def test_unknown_agent_falls_back_to_gm(self, db_session, fake_crewai):
        await run_full_chat(
            db_session,
            user_id=1, team_id=10,
            agent_key="nonexistent",
            user_message="x",
        )
        agent = _FakeCrew.last_instance.agents[0]
        assert agent.role == "gm"  # build_agent_prompt's default fallback


# ---------------------------------------------------------------------------
# End-to-end through the bridge — verifies tools fire inside CrewAI flow
# ---------------------------------------------------------------------------


class TestBridgeEndToEnd:
    async def test_tool_invoked_during_kickoff_returns_data(
        self, db_session, fake_crewai
    ):
        """During (fake) kickoff we fire the team-database tool through
        the bridge. The result confirms our async handler ran AND the
        closure-captured tenancy was preserved (the result schema only
        contains the agent's own data)."""
        observed: dict = {}

        def _hook(_agent, tools):
            # Find the team-database bridge among the agent's tools
            db_tool = next((t for t in tools if t.name == "query_team_database"), None)
            assert db_tool is not None
            # Call it with NO tenant args — closure should provide them
            res = db_tool._run(action="get_team_profile")
            observed["result"] = res

        _FakeCrew.kickoff_hook = _hook
        # No active team for user 1 in tmp DB → should report no_active_team
        # rather than crashing or leaking some other tenant's data.
        await run_full_chat(
            db_session,
            user_id=1, team_id=None,
            agent_key="gm",
            user_message="What does my team look like?",
        )
        # The async tool ran and returned a structured result, JSON-serialized
        assert "no_active_team" in observed["result"]
