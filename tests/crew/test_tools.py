"""Tool factory closures + executor — security + correctness.

The most important thing this file proves is that user_id and team_id
cannot be overridden by the LLM. We do that two ways:
  1. `openai_schema()` MUST NOT contain user_id/team_id in `parameters`
  2. Even if a malicious arg dict tries to pass them, the executor
     strips them before calling the handler — and the handler still
     uses the closure-captured values for the actual DB query.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.crew.tools import (
    Tool,
    default_tools_for_agent,
    execute_tool_call,
    make_knowledge_base_tool,
    make_team_database_tool,
)
from src.models.players import Player, PlayerGameStat
from src.models.teams import TeamProfile
from src.models.uploads import Upload
from src.models.users import User

# ---------------------------------------------------------------------------
# Seeding helpers — two coaches, two teams, overlapping player names
# ---------------------------------------------------------------------------


async def _seed_two_coaches(session) -> dict:
    """Coach A owns team A with Doncic #7. Coach B owns team B with also
    a player named 'Doncic' (different jersey). Used to verify the
    closure pins us to the right tenant."""
    coach_a = User(
        email="a@x.com",
        password_hash="x",
        display_name="A",
        subscription_plan="trial",
    )
    coach_b = User(
        email="b@x.com",
        password_hash="x",
        display_name="B",
        subscription_plan="trial",
    )
    session.add_all([coach_a, coach_b])
    await session.flush()

    team_a = TeamProfile(user_id=coach_a.id, team_name="Team A", play_style="motion")
    team_b = TeamProfile(user_id=coach_b.id, team_name="Team B", play_style="zone")
    session.add_all([team_a, team_b])
    await session.flush()

    # Coach A: #7 Doncic, #11 Brad
    p_a1 = Player(
        user_id=coach_a.id, team_id=team_a.id, name="Luka Doncic",
        number=7, position="PG", strengths="vision",
    )
    p_a2 = Player(
        user_id=coach_a.id, team_id=team_a.id, name="Brad Smith",
        number=11, position="C",
    )
    # Coach B: also has a Doncic, different number
    p_b = Player(
        user_id=coach_b.id, team_id=team_b.id, name="Doncic Jr",
        number=23, position="SF",
    )
    session.add_all([p_a1, p_a2, p_b])
    await session.flush()

    # 1 game for coach A's Doncic, 1 game for coach B's Doncic
    g_a = PlayerGameStat(
        user_id=coach_a.id, team_id=team_a.id, player_id=p_a1.id,
        game_date="2026-04-12", opponent="OpA", points=33, ast=8,
    )
    g_b = PlayerGameStat(
        user_id=coach_b.id, team_id=team_b.id, player_id=p_b.id,
        game_date="2026-04-12", opponent="OpB", points=12, ast=2,
    )
    session.add_all([g_a, g_b])
    await session.flush()

    # 1 upload each
    u_a = Upload(
        user_id=coach_a.id, team_id=team_a.id,
        filename="scout_a.pdf", filepath="/tmp/a.pdf",
        category="scouting", description="A's scout report",
        content_cache="extracted text A",
        uploaded_at=datetime.now(UTC),
    )
    u_b = Upload(
        user_id=coach_b.id, team_id=team_b.id,
        filename="scout_b.pdf", filepath="/tmp/b.pdf",
        category="scouting",
        uploaded_at=datetime.now(UTC),
    )
    session.add_all([u_a, u_b])
    await session.flush()

    return {
        "coach_a": coach_a, "coach_b": coach_b,
        "team_a": team_a, "team_b": team_b,
        "p_a1": p_a1, "p_a2": p_a2, "p_b": p_b,
    }


# ---------------------------------------------------------------------------
# Schema invariants — the model must NOT see tenant fields
# ---------------------------------------------------------------------------


class TestSchemaSecurity:
    """Tenant fields never appear in the schema we publish to OpenAI."""

    def test_team_database_schema_excludes_user_id_and_team_id(self, db_session):
        tool = make_team_database_tool(db_session, user_id=1, team_id=10)
        schema = tool.openai_schema()
        props = schema["function"]["parameters"]["properties"]
        assert "user_id" not in props
        assert "team_id" not in props

    def test_team_database_schema_disallows_extra_fields(self, db_session):
        tool = make_team_database_tool(db_session, user_id=1, team_id=10)
        schema = tool.openai_schema()
        # additionalProperties:false stops a creative LLM from sneaking
        # `user_id` past the schema validator.
        assert schema["function"]["parameters"]["additionalProperties"] is False

    def test_knowledge_base_schema_excludes_tenant_fields(self, db_session):
        tool = make_knowledge_base_tool(db_session, user_id=1, team_id=10)
        props = tool.openai_schema()["function"]["parameters"]["properties"]
        assert "user_id" not in props
        assert "team_id" not in props

    def test_default_agent_tools_returns_4_tools_for_scout(self, db_session):
        """Scout gets 4 tools: team DB, KB, research_external_team, and
        team_schedule (Phase 15). v1 backend/agents.py gave the scout
        `query_team_db + search_kb + research_tool`; Phase 15 added a
        calendar tool to every specialist."""
        tools = default_tools_for_agent(
            "scout", db_session, user_id=1, team_id=10,
        )
        names = {t.name for t in tools}
        assert names == {
            "query_team_database",
            "search_knowledge_base",
            "research_external_team",
            "team_schedule",
        }

    def test_gm_does_not_get_research_tool(self, db_session):
        """GM delegates to specialists in v1, doesn't research himself."""
        tools = default_tools_for_agent(
            "gm", db_session, user_id=1, team_id=10,
        )
        names = {t.name for t in tools}
        assert "research_external_team" not in names

    def test_unknown_agent_returns_empty_toolset(self, db_session):
        tools = default_tools_for_agent(
            "nonexistent", db_session, user_id=1, team_id=10,
        )
        assert tools == []


# ---------------------------------------------------------------------------
# query_team_database — happy path
# ---------------------------------------------------------------------------


class TestQueryTeamDatabase:
    async def test_get_team_profile_returns_active_team(self, db_session):
        seeded = await _seed_two_coaches(db_session)
        tool = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        result = await tool.handler(action="get_team_profile")
        assert result["team_name"] == "Team A"
        assert result["play_style"] == "motion"

    async def test_get_team_profile_no_active_team(self, db_session):
        tool = make_team_database_tool(db_session, user_id=1, team_id=None)
        result = await tool.handler(action="get_team_profile")
        assert result == {"error": "no_active_team"}

    async def test_list_roster_returns_active_players(self, db_session):
        seeded = await _seed_two_coaches(db_session)
        tool = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        result = await tool.handler(action="list_roster")
        names = {p["name"] for p in result["players"]}
        assert names == {"Luka Doncic", "Brad Smith"}

    async def test_get_player_details_by_number(self, db_session):
        seeded = await _seed_two_coaches(db_session)
        tool = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        result = await tool.handler(action="get_player_details", player_name="#7")
        assert result["player"]["name"] == "Luka Doncic"
        assert result["player"]["number"] == 7

    async def test_get_player_details_by_partial_name(self, db_session):
        seeded = await _seed_two_coaches(db_session)
        tool = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        result = await tool.handler(action="get_player_details", player_name="doncic")
        assert result["player"]["name"] == "Luka Doncic"

    async def test_get_player_details_missing_returns_error(self, db_session):
        seeded = await _seed_two_coaches(db_session)
        tool = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        result = await tool.handler(
            action="get_player_details", player_name="nobody",
        )
        assert result["error"] == "player_not_found"

    async def test_list_recent_games_returns_box_scores(self, db_session):
        seeded = await _seed_two_coaches(db_session)
        tool = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        result = await tool.handler(
            action="list_recent_games", player_name="Doncic", limit=5,
        )
        assert result["player"]["name"] == "Luka Doncic"
        assert len(result["games"]) == 1
        assert result["games"][0]["points"] == 33

    async def test_list_uploads_returns_files(self, db_session):
        seeded = await _seed_two_coaches(db_session)
        tool = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        result = await tool.handler(action="list_uploads")
        files = {u["filename"] for u in result["uploads"]}
        assert files == {"scout_a.pdf"}

    async def test_unknown_action_returns_error(self, db_session):
        tool = make_team_database_tool(db_session, user_id=1, team_id=10)
        result = await tool.handler(action="rm_rf")
        assert result["error"].startswith("unknown action")


# ---------------------------------------------------------------------------
# Cross-tenant isolation — the closure pins us to coach A
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    async def test_roster_only_shows_my_players(self, db_session):
        """Coach A's tool returns only Coach A's roster, never B's."""
        seeded = await _seed_two_coaches(db_session)
        tool_a = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        result = await tool_a.handler(action="list_roster")
        names = {p["name"] for p in result["players"]}
        assert "Doncic Jr" not in names  # That's coach B's player

    async def test_player_lookup_only_finds_my_player(self, db_session):
        """Both coaches have a 'Doncic' — coach A's tool MUST return only
        the one she owns, even though B's "Doncic Jr" matches the substring."""
        seeded = await _seed_two_coaches(db_session)
        tool_a = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        result = await tool_a.handler(
            action="get_player_details", player_name="doncic",
        )
        assert result["player"]["id"] == seeded["p_a1"].id
        assert result["player"]["id"] != seeded["p_b"].id

    async def test_uploads_only_show_my_files(self, db_session):
        seeded = await _seed_two_coaches(db_session)
        tool_b = make_team_database_tool(
            db_session, user_id=seeded["coach_b"].id, team_id=seeded["team_b"].id,
        )
        result = await tool_b.handler(action="list_uploads")
        files = {u["filename"] for u in result["uploads"]}
        assert files == {"scout_b.pdf"}  # never scout_a.pdf

    async def test_executor_strips_injected_user_id(self, db_session):
        """Even if a creative LLM tries `arguments={"user_id": <coach_b>, ...}`
        the executor strips tenant fields before calling the handler. The
        handler still uses its closure-captured user_id (coach A) for the
        actual query — so coach B's player is unreachable."""
        seeded = await _seed_two_coaches(db_session)
        tool_a = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        # Pretend the LLM emitted args trying to override tenancy
        result = await execute_tool_call(
            [tool_a],
            "query_team_database",
            {
                "user_id": seeded["coach_b"].id,
                "team_id": seeded["team_b"].id,
                "action": "list_roster",
            },
        )
        names = {p["name"] for p in result["players"]}
        # Still Coach A's roster — closure won, not the injected args
        assert names == {"Luka Doncic", "Brad Smith"}


# ---------------------------------------------------------------------------
# Executor edge cases
# ---------------------------------------------------------------------------


class TestExecutor:
    async def test_unknown_tool_name(self, db_session):
        tool = make_team_database_tool(db_session, user_id=1, team_id=10)
        result = await execute_tool_call([tool], "delete_everything", {})
        assert result["error"] == "unknown tool: delete_everything"

    async def test_arguments_as_json_string(self, db_session):
        seeded = await _seed_two_coaches(db_session)
        tool = make_team_database_tool(
            db_session, user_id=seeded["coach_a"].id, team_id=seeded["team_a"].id,
        )
        # OpenAI sends `arguments` as a JSON string on the wire
        result = await execute_tool_call(
            [tool], "query_team_database", '{"action": "get_team_profile"}',
        )
        assert result["team_name"] == "Team A"

    async def test_arguments_malformed_json_returns_error(self, db_session):
        tool = make_team_database_tool(db_session, user_id=1, team_id=10)
        result = await execute_tool_call(
            [tool], "query_team_database", "{not valid json",
        )
        assert "bad JSON" in result["error"]

    async def test_handler_exception_caught(self, db_session):
        async def boom(**_kwargs):
            raise RuntimeError("kaboom")

        tool = Tool(
            name="bad_tool",
            description="x",
            parameters={"type": "object"},
            handler=boom,
        )
        result = await execute_tool_call([tool], "bad_tool", {})
        assert result["error"] == "kaboom"


# ---------------------------------------------------------------------------
# Knowledge base stub
# ---------------------------------------------------------------------------


class TestKnowledgeBaseTool:
    """The wrapper now talks to a real KnowledgeBase (with an injected
    fake KB for tests). Earlier batches' "stub" assertion is replaced
    with end-to-end search behavior."""

    async def test_empty_kb_returns_no_matches(self, db_session):

        class _StubKb:
            async def search(self, q, limit=5):
                return []

        tool = make_knowledge_base_tool(
            db_session, user_id=1, team_id=10, kb=_StubKb(),
        )
        result = await tool.handler(query="anything")
        assert result["available"] is True
        assert result["results"] == []
        assert "no matches" in result["message"].lower()

    async def test_kb_returns_hits_as_dicts(self, db_session):
        from src.crew.knowledge_base import KbHit

        class _StubKb:
            async def search(self, q, limit=5):
                return [KbHit(id="d1", document="P&R drills", distance=0.1)]

        tool = make_knowledge_base_tool(
            db_session, user_id=1, team_id=10, kb=_StubKb(),
        )
        result = await tool.handler(query="pick and roll")
        assert result["available"] is True
        assert len(result["results"]) == 1
        assert result["results"][0]["document"] == "P&R drills"
        assert result["results"][0]["distance"] == 0.1

    async def test_kb_failure_does_not_crash(self, db_session):
        class _BoomKb:
            async def search(self, q, limit=5):
                raise RuntimeError("chroma offline")

        tool = make_knowledge_base_tool(
            db_session, user_id=1, team_id=10, kb=_BoomKb(),
        )
        result = await tool.handler(query="anything")
        assert result["available"] is False
        assert "chroma offline" in result["message"]

    async def test_empty_query_returns_empty_results(self, db_session):
        class _StubKb:
            async def search(self, q, limit=5):
                return []

        tool = make_knowledge_base_tool(
            db_session, user_id=1, team_id=10, kb=_StubKb(),
        )
        result = await tool.handler(query="")
        assert result["available"] is True
        assert result["results"] == []
