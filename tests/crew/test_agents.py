"""Agent system-prompt registry — verifies the 5 v1 personas + composition."""

from __future__ import annotations

import pytest

from src.crew.agents import (
    AGENTS,
    DEFAULT_AGENT,
    LINEUP_RULES,
    MULTI_TEAM_DATA_RULES,
    build_agent_prompt,
)


class TestAgentRegistry:
    def test_five_agents_present(self):
        # Brad / Hunter / Nexus / Vance / Williams (matches v1 plan).
        assert set(AGENTS) == {"gm", "scout", "analytics", "tactics", "training"}

    def test_default_agent_is_gm(self):
        assert DEFAULT_AGENT == "gm"

    def test_each_agent_has_display_metadata(self):
        for key, meta in AGENTS.items():
            assert "name" in meta and meta["name"]
            assert "role" in meta and meta["role"]
            assert "specialty" in meta and meta["specialty"]


class TestBuildAgentPrompt:
    def test_unknown_agent_falls_back_to_default(self):
        key, prompt = build_agent_prompt("nonexistent")
        assert key == DEFAULT_AGENT  # gm

    def test_none_agent_falls_back_to_default(self):
        key, prompt = build_agent_prompt(None)
        assert key == DEFAULT_AGENT

    def test_gm_prompt_includes_brad_persona(self):
        _, prompt = build_agent_prompt("gm")
        assert "Brad" in prompt or "GM" in prompt or "General Manager" in prompt

    def test_scout_prompt_includes_hunter_or_scout_persona(self):
        _, prompt = build_agent_prompt("scout")
        assert "Hunter" in prompt or "scout" in prompt.lower()

    @pytest.mark.parametrize("key", ["gm", "scout", "analytics", "tactics", "training"])
    def test_every_agent_includes_global_rule_blocks(self, key):
        _, prompt = build_agent_prompt(key)
        # Sample a unique signature from each rule block — ensures all 4
        # blocks made it into the assembled prompt.
        assert "PLAYER-TEAM BINDING" in prompt    # MULTI_TEAM_DATA_RULES
        assert "GAME RESULT — HARD RULE" in prompt  # GAME_RESULT_RULES
        assert "LINEUP COMPOSITION" in prompt    # LINEUP_RULES
        assert "ACCURACY RULES" in prompt        # ACCURACY_RULES

    def test_team_context_is_appended(self):
        _, prompt = build_agent_prompt("gm", team_context="My Team — Roster: #7 Doncic")
        assert "My Team" in prompt
        assert "Doncic" in prompt
        assert "YOUR TEAM CONTEXT" in prompt

    def test_season_header_includes_current_season(self):
        from src.crew.season import current_season
        _, prompt = build_agent_prompt("gm")
        assert current_season() in prompt

    def test_resolved_key_returned_for_each_specialist(self):
        for key in ("scout", "analytics", "tactics", "training"):
            resolved, _ = build_agent_prompt(key)
            assert resolved == key
