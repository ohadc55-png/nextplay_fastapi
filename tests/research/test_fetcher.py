"""4-layer fetcher chain — content sniffing + layer-fallback behavior.

We don't make real HTTP calls. Each layer is patched independently so
we can verify:
  - layer 1 success → return immediately
  - layer 1 thin/blocked → try layer 2
  - all 4 layers fail → return BLOCKED notice
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.research import fetcher


class TestUsableHeuristics:
    def test_bot_gate_marker_caught(self):
        assert fetcher._looks_like_bot_gate(
            "Please verify you are human by completing the action below"
        )

    def test_cloudflare_caught(self):
        assert fetcher._looks_like_bot_gate(
            "Cloudflare is checking your browser, please wait..."
        )

    def test_real_content_passes(self):
        assert not fetcher._looks_like_bot_gate(
            "Maccabi Tel Aviv defeated Real Madrid 78-73 last night."
        )

    def test_404_caught(self):
        assert fetcher._looks_like_404("404 Not Found - The page you requested doesn't exist")

    def test_nav_only_caught(self):
        nav = (
            "Home About Contact Privacy Terms Login Sign up Menu Search " * 10
        )
        assert fetcher._looks_like_nav_only(nav)

    def test_real_article_not_nav(self):
        article = (
            "Maccabi Tel Aviv had a strong third quarter, outscoring Real Madrid "
            "22-14 to take a 12-point lead into the fourth. Their defense forced "
            "5 turnovers in that stretch, and they shot 8/12 from the field. "
            "Coach Spahija praised the team's energy after the game, noting that "
            "their bench rotation was key to maintaining the run. "
        ) * 5
        assert not fetcher._looks_like_nav_only(article)


class TestFetchChain:
    """Each test patches the sync layer functions to force a specific
    fallback path."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        # Each test gets a fresh patch context; fetcher is stateless.
        yield

    async def test_layer_1_success_short_circuits(self):
        with patch.object(fetcher, "_jina_get_sync", return_value="Real article content " * 50) as p1, \
             patch.object(fetcher, "_scrape_tool_sync") as p3, \
             patch.object(fetcher, "_playwright_sync") as p4:
            content = await fetcher.fetch_webpage("https://www.espn.com/article")
            assert "Real article content" in content
            assert p1.called
            assert not p3.called
            assert not p4.called

    async def test_layer_1_thin_falls_through_to_layer_2(self):
        # Layer 1 returns Cloudflare gate; layer 2 returns real content
        responses = iter([
            "Cloudflare is checking your browser",       # layer 1
            "Real article content " * 50,                # layer 2
        ])
        with patch.object(
            fetcher, "_jina_get_sync",
            side_effect=lambda *a, **kw: next(responses),
        ) as p1, \
             patch.object(fetcher, "_scrape_tool_sync") as p3:
            content = await fetcher.fetch_webpage("https://www.espn.com/article")
            assert "Real article content" in content
            assert p1.call_count == 2  # default + browser engine
            assert not p3.called

    async def test_all_layers_fail_returns_blocked_notice(self):
        with patch.object(fetcher, "_jina_get_sync", return_value="Cloudflare access denied"), \
             patch.object(fetcher, "_scrape_tool_sync", return_value=""), \
             patch.object(fetcher, "_playwright_sync", return_value=""):
            content = await fetcher.fetch_webpage("https://blocked-site.com/x")
            # Byte-for-byte v1 BLOCKED prefix — Scout prompt parses this string
            assert content.startswith("BLOCKED:")
            assert "search_the_internet_with_serper" in content

    async def test_invalid_url_rejected(self):
        content = await fetcher.fetch_webpage("not-a-url")
        assert "Invalid URL" in content

    async def test_layer_4_skipped_when_env_disabled(self, monkeypatch):
        """ENABLE_PLAYWRIGHT_FALLBACK=0 (Railway prod) — layer 4 should
        not even attempt to import Playwright."""
        from src.core.config import settings

        monkeypatch.setattr(settings, "ENABLE_PLAYWRIGHT_FALLBACK", "0")

        # All upstream layers fail. Playwright must NOT be invoked.
        with patch.object(fetcher, "_jina_get_sync", return_value="cloudflare"), \
             patch.object(fetcher, "_scrape_tool_sync", return_value=""):
            content = await fetcher.fetch_webpage("https://blocked-site.com/x")
            assert content.startswith("BLOCKED:")
