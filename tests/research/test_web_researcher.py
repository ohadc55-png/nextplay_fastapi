"""WebResearcher orchestrator — cache integration + URL hint flow.

The headline test here is the cross-coach isolation: two coaches
asking the same question in the same hour MUST get separate cache
entries and (in the absence of a real shared upstream call) MUST
each hit the pipeline once."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.research import cache as research_cache
from src.research import fetcher as fetcher_module
from src.research.web_researcher import ResearchRequest, WebResearcher


class _FakeSerperResponse:
    def __init__(self, payload: dict):
        self.ok = True
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    research_cache.clear()
    yield
    research_cache.clear()


# Patch the upstream fetcher so tests don't make real HTTP calls.
_FETCH_PATCH = patch.object(
    fetcher_module, "_jina_get_sync",
    return_value="Maccabi Tel Aviv lineup analysis. " * 200,
)


class TestCacheIntegration:
    async def test_second_call_same_coach_is_cache_hit(self):
        """Two requests from the same coach for the same URL in the same
        hour should result in only ONE pipeline run; the second is served
        from cache."""
        researcher = WebResearcher()
        url = "https://www.espn.com/scout-report"
        with _FETCH_PATCH as p:
            r1 = await researcher.research(ResearchRequest(
                user_id=1, team_id=10,
                query="scout maccabi", url_hint=url,
            ))
            r2 = await researcher.research(ResearchRequest(
                user_id=1, team_id=10,
                query="scout maccabi", url_hint=url,
            ))
        assert r1.cache_hit is False
        assert r2.cache_hit is True
        # Jina should have been called exactly twice (once for default + once
        # for browser engine? no — the response is "usable" so layer 1 wins)
        # for the FIRST request. Second request hits cache → 0 more calls.
        assert p.call_count == 1

    async def test_second_call_different_coach_is_cache_miss(self):
        """The bug fix in action — Coach B's identical query MUST trigger
        a fresh pipeline run, not return Coach A's cached result."""
        researcher = WebResearcher()
        url = "https://www.espn.com/scout-report"
        with _FETCH_PATCH as p:
            r_a = await researcher.research(ResearchRequest(
                user_id=1, team_id=10,
                query="scout maccabi", url_hint=url,
            ))
            r_b = await researcher.research(ResearchRequest(
                user_id=2, team_id=10,  # different coach
                query="scout maccabi", url_hint=url,
            ))
        assert r_a.cache_hit is False
        assert r_b.cache_hit is False
        # Jina was called twice — once per coach, no leak.
        assert p.call_count == 2

    async def test_different_team_is_cache_miss(self):
        """Same coach, different team — different scout context. Must
        re-fetch, can't share cache."""
        researcher = WebResearcher()
        url = "https://www.espn.com/scout-report"
        with _FETCH_PATCH as p:
            await researcher.research(ResearchRequest(
                user_id=1, team_id=10,
                query="scout maccabi", url_hint=url,
            ))
            r2 = await researcher.research(ResearchRequest(
                user_id=1, team_id=20,  # different team
                query="scout maccabi", url_hint=url,
            ))
        assert r2.cache_hit is False
        assert p.call_count == 2


class TestUrlHintFlow:
    async def test_url_hint_with_real_content_returns_summary(self):
        researcher = WebResearcher()
        with _FETCH_PATCH:
            r = await researcher.research(ResearchRequest(
                user_id=1, team_id=10,
                query="scout maccabi",
                url_hint="https://www.espn.com/scout-report",
            ))
        assert r.cache_hit is False
        assert r.urls_fetched == ["https://www.espn.com/scout-report"]
        assert "Maccabi Tel Aviv" in r.summary
        assert len(r.sources) == 1
        # ESPN is tier 2
        assert r.sources[0].tier == 2
        assert r.confidence_overall == "medium"

    async def test_url_hint_blocked_falls_back_to_search(self, monkeypatch):
        """When the URL hint is blocked AND search has no API key, the
        pipeline runs Plan but bails when Search returns []. Friendly
        'no results' message — not a 500."""
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "")

        # Plan still runs (uses OpenAI). Stub the response.
        from src.research import web_researcher as wr_module

        plan_resp = SimpleNamespace(
            model="gpt-4o-mini",
            choices=[SimpleNamespace(message=SimpleNamespace(content=(
                '{"queries": ["site:basketball-reference.com fallback"]}'
            )))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

        async def _fake_create(**_kwargs):
            return plan_resp

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_fake_create)),
        )

        researcher = WebResearcher()
        with patch.object(wr_module, "get_client", return_value=fake_client), \
             patch.object(fetcher_module, "_jina_get_sync", return_value="cloudflare"), \
             patch.object(fetcher_module, "_scrape_tool_sync", return_value=""), \
             patch.object(fetcher_module, "_playwright_sync", return_value=""):
            r = await researcher.research(ResearchRequest(
                user_id=1, team_id=10,
                query="scout opponent",
                url_hint="https://blocked-site.com/page",
            ))
        assert r.cache_hit is False
        assert r.confidence_overall == "low"
        # URL hint was tried and went into urls_fetched
        assert "https://blocked-site.com/page" in r.urls_fetched
        # Search returned empty → friendly message
        assert "no results" in r.summary.lower() or "rephrasing" in r.summary.lower()

    async def test_no_url_hint_with_no_serper_emits_friendly_msg(self, monkeypatch):
        """Without a SERPER_API_KEY (test env), Plan emits queries but
        Search returns nothing. The pipeline should degrade to a
        'share a link' message rather than crashing."""
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "")

        # Stage 1 — Plan returns a non-empty plan via the fake OpenAI
        from unittest.mock import AsyncMock

        from src.research import web_researcher as wr_module

        plan_response = SimpleNamespace(
            model="gpt-4o-mini",
            choices=[SimpleNamespace(message=SimpleNamespace(content=(
                '{"queries": ["site:basketball-reference.com Maccabi 2026"], '
                '"entities_to_verify": ["Maccabi Tel Aviv"]}'
            )))],
            usage=SimpleNamespace(prompt_tokens=200, completion_tokens=80),
        )
        fake_create = AsyncMock(return_value=plan_response)
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
        )
        researcher = WebResearcher()
        with patch.object(wr_module, "get_client", return_value=fake_client):
            r = await researcher.research(ResearchRequest(
                user_id=1, team_id=10,
                query="scout maccabi tel aviv",
                url_hint=None,
            ))
        # Plan ran (queries logged); Search returned empty (no key)
        assert "Maccabi" in r.queries_run[0]
        assert "no results" in r.summary.lower() or "rephrasing" in r.summary.lower()
        assert r.confidence_overall == "low"

    async def test_full_search_flow_picks_top_url_and_fetches(self, monkeypatch):
        """Plan → Serper → Triage → Fetch — all stages wired, mocked
        end-to-end. Verifies the Triage-picked URL gets fetched and
        the result includes its content as summary."""
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "test-key")

        from src.research import web_researcher as wr_module

        # Stage 1 — Plan output (3 queries)
        plan_resp = SimpleNamespace(
            model="gpt-4o-mini",
            choices=[SimpleNamespace(message=SimpleNamespace(content=(
                '{"queries": ['
                '"site:basketball-reference.com Maccabi 2026",'
                '"site:euroleaguebasketball.net Maccabi"'
                '], "entities_to_verify": ["Maccabi Tel Aviv"]}'
            )))],
            usage=SimpleNamespace(prompt_tokens=200, completion_tokens=60),
        )
        # Stage 3 — Triage picks 1 URL
        triage_resp = SimpleNamespace(
            model="gpt-4o-mini",
            choices=[SimpleNamespace(message=SimpleNamespace(content=(
                '{"top_urls_to_fetch": ['
                '{"url": "https://www.basketball-reference.com/euroleague/maccabi/2026.html",'
                ' "tier": 1, "reason": "primary stat page"}'
                '], "should_refine": false}'
            )))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=80),
        )
        responses = iter([plan_resp, triage_resp])

        async def _fake_create(**_kwargs):
            return next(responses)

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_fake_create)),
        )

        # Stage 2 — Serper returns 2 snippets
        def _fake_serper(*_args, **kwargs):
            return _FakeSerperResponse(payload={"organic": [
                {"title": "Maccabi 2026", "snippet": "great team",
                 "link": "https://www.basketball-reference.com/euroleague/maccabi/2026.html"},
                {"title": "EL Maccabi", "snippet": "stats",
                 "link": "https://www.euroleaguebasketball.net/maccabi"},
            ]})

        # Stage 4 — fetch returns real content
        with patch.object(wr_module, "get_client", return_value=fake_client), \
             patch("requests.post", side_effect=_fake_serper), \
             patch.object(fetcher_module, "_jina_get_sync",
                          return_value="Maccabi Tel Aviv 2025-26 box scores. " * 100):
            researcher = WebResearcher()
            r = await researcher.research(ResearchRequest(
                user_id=1, team_id=10,
                query="scout maccabi tel aviv",
                url_hint=None,
            ))

        # Plan + Triage queries logged
        assert len(r.queries_run) == 2
        # Top URL was fetched
        assert r.urls_fetched == ["https://www.basketball-reference.com/euroleague/maccabi/2026.html"]
        # Source has tier 1 (basketball-reference.com)
        assert len(r.sources) == 1
        assert r.sources[0].tier == 1
        # Summary includes the content
        assert "Maccabi" in r.summary
        # Confidence reflects tier-1 source
        assert r.confidence_overall == "medium"

    async def test_url_hint_failure_falls_back_to_search(self, monkeypatch):
        """Stage 0 fails (URL is bot-protected) → derive bias_domain →
        Stage 1+ runs Plan+Search+Triage+Fetch."""
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "test-key")

        from src.research import web_researcher as wr_module

        plan_resp = SimpleNamespace(
            model="gpt-4o-mini",
            choices=[SimpleNamespace(message=SimpleNamespace(content=(
                '{"queries": ["site:basketball-reference.com test"]}'
            )))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
        triage_resp = SimpleNamespace(
            model="gpt-4o-mini",
            choices=[SimpleNamespace(message=SimpleNamespace(content=(
                '{"top_urls_to_fetch": []}'  # empty → falls back to ranked snippets
            )))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
        responses = iter([plan_resp, triage_resp])

        async def _fake_create(**_kwargs):
            return next(responses)

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_fake_create)),
        )

        # Layer 1+2 always return cloudflare for the FIRST URL (the hint),
        # but layer 1 returns content for subsequent URLs (the search results)
        call_log = {"n": 0}

        def _jina_responses(url, *, browser_engine=False):
            call_log["n"] += 1
            # First fetch is the URL hint (blocked); subsequent are real content
            if "blocked-site" in url:
                return "cloudflare access denied"
            return "Real basketball-reference.com content. " * 100

        def _fake_serper(*_args, **kwargs):
            return _FakeSerperResponse(payload={"organic": [
                {"title": "BR", "snippet": "x",
                 "link": "https://www.basketball-reference.com/euroleague/maccabi/2026.html"},
            ]})

        with patch.object(wr_module, "get_client", return_value=fake_client), \
             patch("requests.post", side_effect=_fake_serper), \
             patch.object(fetcher_module, "_jina_get_sync", side_effect=_jina_responses), \
             patch.object(fetcher_module, "_scrape_tool_sync", return_value=""), \
             patch.object(fetcher_module, "_playwright_sync", return_value=""):
            researcher = WebResearcher()
            r = await researcher.research(ResearchRequest(
                user_id=1, team_id=10,
                query="scout maccabi",
                url_hint="https://blocked-site.com/maccabi",
            ))

        # The URL hint was tried (and failed); search-based URL was fetched too
        assert "https://blocked-site.com/maccabi" in r.urls_fetched
        # Plus a search-derived URL was fetched
        assert any("basketball-reference" in u for u in r.urls_fetched)
        # We got usable content
        assert "basketball-reference" in r.summary

    async def test_pipeline_exception_returns_friendly_error(self):
        """If something deep in the pipeline blows up, the user gets a
        readable summary, not a 500."""
        from src.research import web_researcher as wr_module

        researcher = WebResearcher()
        # Patch the bound reference at the use site — `from … import
        # fetch_webpage` rebinds at import time, so patching the source
        # module doesn't affect existing bindings.
        with patch.object(
            wr_module, "fetch_webpage",
            side_effect=RuntimeError("simulated"),
        ):
            r = await researcher.research(ResearchRequest(
                user_id=1, team_id=10,
                query="scout maccabi",
                url_hint="https://www.espn.com/x",
            ))
        assert r.confidence_overall == "low"
        # The summary explains what happened so the agent can degrade.
        assert "internal error" in r.summary.lower() or "couldn't" in r.summary.lower()
