"""Research cache tests — the cross-coach leak fix is the headline.

Master prompt §3.1 mandates that every cache entry be keyed by
(user_id, team_id, query, level_hint, url_hint, hour_bucket). These
tests verify the behavior end-to-end:

  - Same coach, same query, same hour → cache hit
  - Different coach, same query, same hour → cache MISS (this is the bug fix)
  - Different team, same coach, same query → cache MISS
  - Different hour, same everything → cache MISS

If any of these regress, the multi-tenancy invariant is broken.
"""

from __future__ import annotations

import pytest

from src.research import cache as research_cache
from src.research.models import ResearchResult


@pytest.fixture(autouse=True)
def _clear_cache():
    research_cache.clear()
    yield
    research_cache.clear()


def _make_result(summary: str = "ok") -> ResearchResult:
    return ResearchResult(summary=summary, confidence_overall="medium")


class TestTenantIsolation:
    """The bug fix lives here — never let coach B see coach A's cache."""

    def test_same_coach_same_query_returns_cached(self):
        r = _make_result("Coach A's report")
        research_cache.store(
            user_id=1, team_id=10,
            query="scout maccabi",
            level_hint=None, url_hint=None,
            result=r, hour=42,
        )
        hit = research_cache.lookup(
            user_id=1, team_id=10,
            query="scout maccabi",
            level_hint=None, url_hint=None,
            hour=42,
        )
        assert hit is r

    def test_different_user_id_misses(self):
        """The cross-coach leak fix. Coach B asks the same question in
        the same hour and gets nothing — cache must not surface A's
        report."""
        research_cache.store(
            user_id=1, team_id=10,
            query="scout maccabi",
            level_hint=None, url_hint=None,
            result=_make_result("A's report"),
            hour=42,
        )
        hit = research_cache.lookup(
            user_id=2, team_id=10,
            query="scout maccabi",
            level_hint=None, url_hint=None,
            hour=42,
        )
        assert hit is None

    def test_different_team_id_misses(self):
        """Same coach, different team — different cache entry. A coach
        can run two scout flows for two teams and they don't interfere."""
        research_cache.store(
            user_id=1, team_id=10,
            query="scout maccabi",
            level_hint=None, url_hint=None,
            result=_make_result("Team A's report"),
            hour=42,
        )
        hit = research_cache.lookup(
            user_id=1, team_id=20,  # different team
            query="scout maccabi",
            level_hint=None, url_hint=None,
            hour=42,
        )
        assert hit is None

    def test_team_id_none_matches_team_id_none(self):
        """Coach without an active team caches at team_id=None — that
        bucket is its own namespace, separate from any team-bound entry."""
        research_cache.store(
            user_id=1, team_id=None,
            query="generic basketball question",
            level_hint=None, url_hint=None,
            result=_make_result("answer"),
            hour=42,
        )
        # Same null team → hit
        assert research_cache.lookup(
            user_id=1, team_id=None,
            query="generic basketball question",
            level_hint=None, url_hint=None, hour=42,
        ) is not None
        # team_id=10 should be a miss because keys don't match
        assert research_cache.lookup(
            user_id=1, team_id=10,
            query="generic basketball question",
            level_hint=None, url_hint=None, hour=42,
        ) is None


class TestKeyNormalization:
    """The key normalizer trims and lowercases — same logical query
    should hit even with whitespace/case noise."""

    def test_query_case_insensitive(self):
        research_cache.store(
            user_id=1, team_id=10, query="Scout Maccabi",
            level_hint=None, url_hint=None,
            result=_make_result(), hour=42,
        )
        hit = research_cache.lookup(
            user_id=1, team_id=10, query="SCOUT MACCABI",
            level_hint=None, url_hint=None, hour=42,
        )
        assert hit is not None

    def test_query_whitespace_stripped(self):
        research_cache.store(
            user_id=1, team_id=10, query="scout maccabi",
            level_hint=None, url_hint=None,
            result=_make_result(), hour=42,
        )
        hit = research_cache.lookup(
            user_id=1, team_id=10, query="  scout maccabi   ",
            level_hint=None, url_hint=None, hour=42,
        )
        assert hit is not None


class TestHourInvalidation:
    def test_different_hour_misses(self):
        research_cache.store(
            user_id=1, team_id=10, query="scout maccabi",
            level_hint=None, url_hint=None,
            result=_make_result(), hour=42,
        )
        hit = research_cache.lookup(
            user_id=1, team_id=10, query="scout maccabi",
            level_hint=None, url_hint=None, hour=43,
        )
        assert hit is None


class TestEviction:
    def test_fifo_evicts_oldest_at_capacity(self, monkeypatch):
        # Shrink the cap so the test doesn't have to insert 257 entries.
        monkeypatch.setattr(research_cache, "_CACHE_MAX", 3)
        for i in range(3):
            research_cache.store(
                user_id=1, team_id=10, query=f"q{i}",
                level_hint=None, url_hint=None,
                result=_make_result(f"r{i}"), hour=42,
            )
        assert research_cache.size() == 3
        # Insert one more — the oldest (q0) should be evicted
        research_cache.store(
            user_id=1, team_id=10, query="q3",
            level_hint=None, url_hint=None,
            result=_make_result("r3"), hour=42,
        )
        assert research_cache.size() == 3
        assert research_cache.lookup(
            user_id=1, team_id=10, query="q0",
            level_hint=None, url_hint=None, hour=42,
        ) is None
        assert research_cache.lookup(
            user_id=1, team_id=10, query="q3",
            level_hint=None, url_hint=None, hour=42,
        ) is not None


class TestHourBucket:
    def test_current_hour_bucket_is_int(self):
        bucket = research_cache.current_hour_bucket()
        assert isinstance(bucket, int)
        # Roughly hours since 1970 — anything past 2020 is > 438_000.
        assert bucket > 400_000
