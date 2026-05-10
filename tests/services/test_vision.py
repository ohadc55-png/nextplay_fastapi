"""Vision pipeline — type detection, enriched-message builder, Stage 1+2 calls.

We don't make real Vision calls. The OpenAI client is patched, and each
test asserts on the message payload + response handling logic. Image IO
is exercised by writing a tiny PNG fixture to a tmp_path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.crew import llm as llm_module
from src.services import vision
from src.services.vision import (
    _detect_image_type,
    analyze_image,
    build_two_stage_enriched_message,
    describe_basketball_image,
    get_file_extension,
    is_image,
)

# ---------------------------------------------------------------------------
# Tiny PNG fixture (1x1 pixel, valid header)
# ---------------------------------------------------------------------------

# Minimal valid PNG — 1×1 pixel, fully transparent. Enough for our IO path
# tests without dragging Pillow in.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"  # signature
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def png_path(tmp_path):
    p = tmp_path / "scene.png"
    p.write_bytes(_PNG_BYTES)
    return str(p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_completion(text: str = "IMAGE_TYPE: GAME_SCENE\n1. Two teams on the court."):
    return SimpleNamespace(
        model="gpt-4o",
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text),
        )],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=200),
    )


def _patch_openai(create_mock: AsyncMock):
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock)),
    )
    return (
        patch.object(llm_module, "get_client", return_value=fake_client),
        patch.object(vision, "get_client", return_value=fake_client),
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestPureHelpers:
    @pytest.mark.parametrize("name,expected", [
        ("photo.jpg", True), ("photo.JPEG", True),
        ("court.png", True), ("ball.gif", True), ("scene.webp", True),
        ("doc.pdf", False), ("notes.txt", False), ("playbook.md", False),
        ("", False),
    ])
    def test_is_image(self, name, expected):
        assert is_image(name) is expected

    def test_extension_handles_path(self):
        assert get_file_extension("/tmp/path/x.JPG") == "jpg"
        assert get_file_extension("nopath.PNG") == "png"
        assert get_file_extension("noext") == ""


class TestDetectImageType:
    @pytest.mark.parametrize("description,expected", [
        ("IMAGE_TYPE: STAT_SHEET\nrest of the response", "STAT_SHEET"),
        ("IMAGE_TYPE: GAME_SCENE\n", "GAME_SCENE"),
        ("IMAGE_TYPE: PLAY_DIAGRAM", "PLAY_DIAGRAM"),
        ("IMAGE_TYPE: SHOT_CHART\n1. ...", "SHOT_CHART"),
        ("IMAGE_TYPE: OTHER", "OTHER"),
        ("image_type:  game_scene  ", "GAME_SCENE"),  # case + spacing
    ])
    def test_explicit_tag_parsed(self, description, expected):
        assert _detect_image_type(description) == expected

    def test_missing_tag_defaults_to_game_scene(self):
        # Most common case — Vision returned content without the prefix.
        assert _detect_image_type("Two players in the paint, dark #4 boxing out.") == "GAME_SCENE"

    def test_empty_input_defaults_to_game_scene(self):
        assert _detect_image_type("") == "GAME_SCENE"
        assert _detect_image_type(None) == "GAME_SCENE"


class TestEnrichedMessage:
    """The Stage 2 builder picks the right instruction block per type."""

    def test_game_scene_includes_offense_defense_required_output(self):
        msg = build_two_stage_enriched_message(
            "IMAGE_TYPE: GAME_SCENE\n1. ...",
            "What should we do?",
        )
        assert "WHAT THE OFFENSE IS DOING" in msg
        assert "WHAT THE DEFENSE IS DOING" in msg
        assert "ACTIONABLE TAKEAWAYS" in msg
        # Coach question is included
        assert "What should we do?" in msg
        # Visual analysis is included as ground truth
        assert "VISUAL ANALYSIS" in msg
        assert "ground truth" in msg

    def test_stat_sheet_includes_data_isolation_rule(self):
        msg = build_two_stage_enriched_message(
            "IMAGE_TYPE: STAT_SHEET\nGAME HEADER: ...",
            "Did we win?",
        )
        # The dangerous data-mixing rule must be in the prompt
        assert "DATA SOURCE ISOLATION" in msg
        # Game stats JSON tail required for downstream extraction
        assert "GAME_STATS_JSON" in msg

    def test_play_diagram_picks_diagram_instructions(self):
        msg = build_two_stage_enriched_message(
            "IMAGE_TYPE: PLAY_DIAGRAM\nWhiteboard with X and O...",
            "Should we run this against zone?",
        )
        # Diagram-specific block is selected
        assert "primary read" in msg.lower() or "concept" in msg.lower()
        # Stat-sheet rules absent
        assert "DATA SOURCE ISOLATION" not in msg

    def test_shot_chart_picks_shot_chart_instructions(self):
        msg = build_two_stage_enriched_message(
            "IMAGE_TYPE: SHOT_CHART\nHeatmap shows...",
            "Where is he hot?",
        )
        assert "hot zones" in msg.lower() or "heatmap" in msg.lower()

    def test_unknown_type_falls_back_to_game_scene(self):
        msg = build_two_stage_enriched_message(
            "no IMAGE_TYPE tag at all",
            "What's happening?",
        )
        assert "WHAT THE OFFENSE IS DOING" in msg


# ---------------------------------------------------------------------------
# Stage 1 — describe_basketball_image
# ---------------------------------------------------------------------------


class TestDescribeBasketballImage:
    async def test_returns_extraction_text(self, png_path):
        create = AsyncMock(return_value=_fake_completion(
            "IMAGE_TYPE: GAME_SCENE\n1. PLAYER COUNT: 2\n2. Court zones..."
        ))
        a, b = _patch_openai(create)
        with a, b:
            text = await describe_basketball_image(png_path, "Tactical read?")
        assert "IMAGE_TYPE:" in text
        assert "PLAYER COUNT" in text
        # Verify the extractor system prompt was used (not specialist persona)
        call_kwargs = create.await_args.kwargs
        messages = call_kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert "scene extractor" in messages[0]["content"].lower()
        # Verify image was attached as data URI
        user_content = messages[1]["content"]
        assert isinstance(user_content, list)
        kinds = {part["type"] for part in user_content}
        assert kinds == {"text", "image_url"}
        # Detail set to "high" so model can read jersey numbers
        image_part = next(p for p in user_content if p["type"] == "image_url")
        assert image_part["image_url"]["detail"] == "high"
        assert image_part["image_url"]["url"].startswith("data:image/png;base64,")

    async def test_uses_gpt4o_not_mini(self, png_path):
        """Stage 1 needs Vision — must be gpt-4o (or 4o variant), never mini-only."""
        create = AsyncMock(return_value=_fake_completion())
        a, b = _patch_openai(create)
        with a, b:
            await describe_basketball_image(png_path, "")
        call_kwargs = create.await_args.kwargs
        # Accept gpt-4o or any variant that supports Vision
        assert call_kwargs["model"].startswith("gpt-4")
        # Low temperature for deterministic extraction
        assert call_kwargs["temperature"] <= 0.2

    async def test_user_message_appended_to_extraction_prompt(self, png_path):
        create = AsyncMock(return_value=_fake_completion())
        a, b = _patch_openai(create)
        with a, b:
            await describe_basketball_image(png_path, "What's the defense doing?")
        text_part = next(
            p for p in create.await_args.kwargs["messages"][1]["content"]
            if p["type"] == "text"
        )
        assert "What's the defense doing?" in text_part["text"]

    async def test_failure_propagates(self, tmp_path):
        """No OpenAI mock + nonexistent file → describe raises (the caller
        is expected to catch and fall back to analyze_image)."""
        with pytest.raises(Exception):
            await describe_basketball_image(
                str(tmp_path / "nonexistent.png"), "x"
            )


# ---------------------------------------------------------------------------
# Single-call fallback — analyze_image
# ---------------------------------------------------------------------------


class TestAnalyzeImageFallback:
    async def test_uses_agent_persona_as_system(self, png_path):
        create = AsyncMock(return_value=_fake_completion("Tactical analysis..."))
        a, b = _patch_openai(create)
        agent_prompt = "You are Brad Binn, a no-nonsense GM..."
        with a, b:
            text = await analyze_image(
                png_path,
                agent_prompt=agent_prompt,
                user_message="What do you see?",
            )
        assert "Tactical analysis" in text
        messages = create.await_args.kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == agent_prompt

    async def test_includes_team_context_when_provided(self, png_path):
        create = AsyncMock(return_value=_fake_completion())
        a, b = _patch_openai(create)
        with a, b:
            await analyze_image(
                png_path,
                agent_prompt="You are GM",
                user_message="Read the play",
                team_ctx="Roster: #7 Doncic, #11 Smith",
            )
        text_part = next(
            p for p in create.await_args.kwargs["messages"][1]["content"]
            if p["type"] == "text"
        )
        assert "TEAM CONTEXT" in text_part["text"]
        assert "Doncic" in text_part["text"]

    async def test_failure_returns_friendly_string(self, tmp_path):
        """analyze_image is the LAST line of defense — it must NEVER raise."""
        text = await analyze_image(
            str(tmp_path / "missing.png"),
            agent_prompt="x", user_message="y",
        )
        assert "Error reading image" in text

    async def test_openai_failure_returns_friendly_string(self, png_path):
        """When OpenAI itself raises, analyze_image still returns text."""
        boom = AsyncMock(side_effect=RuntimeError("api down"))
        a, b = _patch_openai(boom)
        with a, b:
            text = await analyze_image(
                png_path,
                agent_prompt="x", user_message="y",
            )
        assert "Error analyzing image" in text
