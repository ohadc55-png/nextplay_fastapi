"""3-layer router — deterministic shortcuts + own-team match + LLM fallback.

Layer 1/2 are pure-Python so they're tested without OpenAI calls. Layer 3
is mocked — we verify it's called only when the deterministic layers
fall through, and that its result is cached so a tab refresh doesn't
re-bill the classifier."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.crew import routing as routing_module
from src.crew.routing import _reset_cache_for_tests, route_query


@pytest.fixture(autouse=True)
def _reset():
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


# ---------------------------------------------------------------------------
# Layer 1 — deterministic shortcuts
# ---------------------------------------------------------------------------

class TestDeterministicLayer:
    async def test_empty_message_returns_default(self):
        assert await route_query("") == "gm"
        assert await route_query("   ") == "gm"

    async def test_url_in_message_routes_to_scout(self):
        agent = await route_query(
            "Scout this team for me: https://stats.fiba.com/team/12"
        )
        assert agent == "scout"

    async def test_research_keyword_in_english_routes_to_scout(self):
        for msg in (
            "Can you scout the Lakers next game?",
            "I need a scouting report on Real Madrid",
            "Look up the current Duke roster",
        ):
            assert await route_query(msg) == "scout"

    async def test_research_keyword_in_hebrew_routes_to_scout(self):
        agent = await route_query("חפש נתונים על מכבי תל אביב")
        assert agent == "scout"

    async def test_stats_vocabulary_routes_to_analytics(self):
        for msg in ("What's our PPG this season?", "Show me the eFG% trend"):
            assert await route_query(msg) == "analytics"

    async def test_practice_vocabulary_routes_to_training(self):
        for msg in (
            "Build me a 90-minute practice plan for tomorrow",
            "Give me a shooting drill for guards",
        ):
            assert await route_query(msg) == "training"

    async def test_tactics_vocabulary_routes_to_tactics(self):
        for msg in (
            "Design a play out of this set",
            "How should we defend their pick and roll?",
        ):
            assert await route_query(msg) == "tactics"


# ---------------------------------------------------------------------------
# Layer 2 — own-team semantic match
# ---------------------------------------------------------------------------

class TestOwnTeamLayer:
    async def test_possessive_marker_routes_to_gm(self):
        agent = await route_query(
            "What about our team's lineup?", team_ctx="(team context here)"
        )
        assert agent == "gm"

    async def test_hebrew_possessive_routes_to_gm(self):
        agent = await route_query("מי השחקנים החזקים שלנו?", team_ctx="")
        assert agent == "gm"

    async def test_roster_player_name_routes_to_gm(self):
        ctx = "Our roster:\n  Yoni Lev #7 (PG)\n  Dan Ohayon #11 (SF)"
        agent = await route_query("How is Dan Ohayon developing?", team_ctx=ctx)
        assert agent == "gm"


# ---------------------------------------------------------------------------
# Layer 3 — LLM classifier (mocked)
# ---------------------------------------------------------------------------

def _fake_llm_response(agent: str, reasoning: str = "matched"):
    import json
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"agent": agent, "reasoning": reasoning})
        ))],
    )


class TestLLMClassifier:
    async def test_falls_back_to_classifier_when_deterministic_misses(self):
        fake_completions = SimpleNamespace(
            create=AsyncMock(return_value=_fake_llm_response("analytics"))
        )
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
        with patch.object(routing_module, "get_client", return_value=fake_client):
            agent = await route_query("Tell me about the rebound battle")
        assert agent == "analytics"
        assert fake_completions.create.await_count == 1

    async def test_classifier_result_is_cached(self):
        fake_completions = SimpleNamespace(
            create=AsyncMock(return_value=_fake_llm_response("tactics"))
        )
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
        with patch.object(routing_module, "get_client", return_value=fake_client):
            a1 = await route_query("Generic ambiguous message")
            a2 = await route_query("Generic ambiguous message")
        assert a1 == a2 == "tactics"
        # Cache means only one network call total
        assert fake_completions.create.await_count == 1

    async def test_classifier_unknown_agent_defaults_to_gm(self):
        fake_completions = SimpleNamespace(
            create=AsyncMock(return_value=_fake_llm_response("imaginary"))
        )
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
        with patch.object(routing_module, "get_client", return_value=fake_client):
            agent = await route_query("Generic ambiguous question")
        assert agent == "gm"

    async def test_classifier_failure_defaults_to_gm(self):
        fake_completions = SimpleNamespace(
            create=AsyncMock(side_effect=RuntimeError("network down"))
        )
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
        with patch.object(routing_module, "get_client", return_value=fake_client):
            agent = await route_query("Generic ambiguous message 2")
        assert agent == "gm"
