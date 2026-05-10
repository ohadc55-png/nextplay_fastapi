"""Source authority — domain trust ranking + URL ranker."""

from __future__ import annotations

import pytest

from src.research.source_authority import rank_urls, url_tier


class TestUrlTier:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.basketball-reference.com/teams/LAL/2026.html", 1),
        ("https://www.sports-reference.com/cbb/schools/duke/men/2026.html", 1),
        ("https://www.kenpom.com/team/Duke", 1),
        ("https://www.fiba.basketball/euroleague", 1),
    ])
    def test_tier_1_domains(self, url, expected):
        assert url_tier(url) == expected

    @pytest.mark.parametrize("url,expected", [
        ("https://www.espn.com/mens-college-basketball", 2),
        ("https://www.basketnews.com/some-article", 2),
        ("https://www.cbssports.com/some-article", 2),
    ])
    def test_tier_2_domains(self, url, expected):
        assert url_tier(url) == expected

    def test_tier_3_domain(self):
        assert url_tier("https://en.wikipedia.org/wiki/Maccabi_Tel_Aviv_B.C.") == 3

    def test_unknown_domain_is_tier_4(self):
        """Open-search model — unknown domains aren't blacklisted, just
        flagged. They get tier 4 so the LLM gets the warning treatment."""
        assert url_tier("https://example.com/article") == 4

    @pytest.mark.parametrize("url", [
        "https://reddit.com/r/nba",
        "https://twitter.com/nba",
        "https://x.com/nba",
        "https://www.youtube.com/watch?v=abc",
        "https://facebook.com/nba",
    ])
    def test_blacklisted_domains_return_none(self, url):
        assert url_tier(url) is None

    def test_empty_url_returns_none(self):
        assert url_tier("") is None


class TestRankUrls:
    def test_sorts_by_tier(self):
        urls = [
            "https://example.com/a",          # tier 4
            "https://www.espn.com/x",          # tier 2
            "https://basketball-reference.com/y",  # tier 1
            "https://en.wikipedia.org/z",      # tier 3
        ]
        ranked = rank_urls(urls)
        assert [t for _, t in ranked] == [1, 2, 3, 4]

    def test_drops_blacklisted(self):
        urls = [
            "https://twitter.com/junk",
            "https://www.espn.com/article",
        ]
        ranked = rank_urls(urls)
        assert len(ranked) == 1
        assert "espn" in ranked[0][0]

    def test_dedupes(self):
        urls = ["https://www.espn.com/x"] * 3
        ranked = rank_urls(urls)
        assert len(ranked) == 1

    def test_respects_limit(self):
        urls = [f"https://example{i}.com/a" for i in range(10)]
        ranked = rank_urls(urls, limit=3)
        assert len(ranked) == 3
