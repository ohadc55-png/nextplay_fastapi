"""Web research orchestrator — async port of `backend/research/web_researcher.py`.

Implements all 8 stages of v1's research pipeline:
  Stage 0 — URL hint try (single fetch + return content)
  Stage 1 — PLAN        (gpt-4o-mini emits a JSON plan)
  Stage 2 — SEARCH      (Serper queries in parallel)
  Stage 3 — TRIAGE      (gpt-4o-mini ranks snippets, picks top URLs)
  Stage 4 — FETCH       (parallel async fetches via Jina-first chain)
  Stage 5 — EXTRACT     (gpt-4o pulls structured findings per page)
  Stage 6 — VERIFY      (cross-source agreement bumps confidence)
  Stage 7 — SYNTHESIZE  (gpt-4o builds the structured scout report)

Multi-tenancy invariants:
  - Cache is keyed by `(user_id, team_id, …)` (the master-prompt §3.1
    bug fix lives in `cache.py`).
  - All stages take `(user_id, team_id)` for cost logging — same tenant
    flows through the entire pipeline.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.crew.llm import get_client, log_response
from src.research import cache as research_cache
from src.research.fetcher import _is_usable, fetch_webpage
from src.research.models import Finding, ResearchResult, Source
from src.research.prompts import (
    TRIAGE_PROMPT,
    get_extract_prompt,
    get_plan_prompt,
    get_synthesize_prompt,
)
from src.research.serper import serper_batch
from src.research.source_authority import TIER_1, TIER_2, rank_urls, url_tier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchRequest:
    """The request shape — explicit user_id+team_id close the cache-key
    bug by making them part of the cache lookup. The LLM cannot inject
    these values; they come from the calling agent context."""
    user_id: int
    team_id: int | None
    query: str
    level_hint: str | None = None
    url_hint: str | None = None


# ---------------------------------------------------------------------------
# Tunables — match v1 (web_researcher.py:33-47)
# ---------------------------------------------------------------------------

MAX_QUERIES_PER_PLAN = 5
MAX_URLS_TO_FETCH = 3
MAX_TOKENS_PLAN = 600
MAX_TOKENS_TRIAGE = 800
MAX_TOKENS_EXTRACT = 3000
MAX_TOKENS_SYNTHESIZE = 2500
EXTRACT_CHAR_CAP = 30000   # per-page input cap (~13K tokens for gpt-4o)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class WebResearcher:
    """8-stage research pipeline — async edition.

    Stage 0 (URL hint fast path) returns the raw page content as
    `summary` for the calling agent to extract from — deliberately
    skips the LLM stages because the coach already pointed at the
    source. Otherwise the full pipeline (Plan → Search → Triage →
    Fetch → Extract → Verify → Synthesize) runs and `tactical_insights`
    is populated with the structured scout report.
    """

    def __init__(self, db: AsyncSession | None = None) -> None:
        # `db` lets us log OpenAI costs through the request's session.
        # When None (e.g. from a CLI tool) we skip the cost log.
        self._db = db

    async def research(self, req: ResearchRequest) -> ResearchResult:
        """Top-level entrypoint. NEVER raises on internal errors —
        returns a ResearchResult with `summary` set to a friendly note."""
        # 1. Cache lookup — keyed by tenant.
        try:
            cached = research_cache.lookup(
                user_id=req.user_id, team_id=req.team_id,
                query=req.query, level_hint=req.level_hint,
                url_hint=req.url_hint,
            )
            if cached is not None:
                # Return a fresh ResearchResult flagged as a cache hit; we
                # don't mutate the cached object because future requests
                # share that reference.
                hit = ResearchResult(
                    summary=cached.summary,
                    findings=list(cached.findings),
                    sources=list(cached.sources),
                    missing=list(cached.missing),
                    confidence_overall=cached.confidence_overall,
                    queries_run=list(cached.queries_run),
                    urls_fetched=list(cached.urls_fetched),
                    refinement_loops=cached.refinement_loops,
                    elapsed_seconds=cached.elapsed_seconds,
                    cache_hit=True,
                    tactical_insights=dict(cached.tactical_insights),
                )
                return hit
        except Exception as e:
            logger.warning("[research] cache lookup failed, running fresh: %s", e)

        # 2. Run pipeline.
        t_start = time.time()
        result = ResearchResult(summary="", confidence_overall="low")
        try:
            await self._run_pipeline(req, result)
        except Exception as e:
            logger.exception("[research] pipeline crashed")
            result.summary = (
                f"Research encountered an internal error: {e}. "
                "I tried but couldn't complete the search this turn."
            )
        result.elapsed_seconds = round(time.time() - t_start, 2)
        result.cache_hit = False

        # 3. Cache the result (only if we found something useful).
        if result.findings or result.summary:
            try:
                research_cache.store(
                    user_id=req.user_id, team_id=req.team_id,
                    query=req.query, level_hint=req.level_hint,
                    url_hint=req.url_hint, result=result,
                )
            except Exception as e:
                logger.debug("[research] cache store failed: %s", e)

        return result

    async def _run_pipeline(self, req: ResearchRequest, result: ResearchResult) -> None:
        """Stage 0 — URL hint fast path; otherwise Stage 1 → 4."""
        bias_domain: str | None = None
        if req.url_hint:
            content = await fetch_webpage(req.url_hint)
            result.urls_fetched.append(req.url_hint)
            if not content.startswith("BLOCKED:") and _is_usable(content):
                self._absorb_fetched_page(req.url_hint, content, result)
                return
            # URL hint failed — derive a bias_domain so Plan can prefer
            # the same source on Serper. Mirrors v1 web_researcher.py:108-116.
            bias_domain = _domain_of(req.url_hint)
            logger.info(
                "[research] Stage 0 URL hint blocked, falling back to search "
                "(bias_domain=%s)", bias_domain,
            )

        # Stage 1 — PLAN
        plan = await self._stage_plan(
            req, bias_domain=bias_domain,
        )
        queries = list(plan.get("queries") or [])[:MAX_QUERIES_PER_PLAN]
        result.queries_run.extend(queries)
        if not queries:
            result.summary = (
                "I couldn't formulate a search plan for that question. "
                "Try rephrasing or sharing a URL."
            )
            result.confidence_overall = "low"
            return

        # Stage 2 — SEARCH
        snippets = await serper_batch(queries, num=10)
        if not snippets:
            result.summary = (
                "Search returned no results. Try sharing a URL with the "
                "team's stats / roster page, or rephrasing the question."
            )
            result.confidence_overall = "low"
            return

        # Stage 3 — TRIAGE
        triage = await self._stage_triage(
            req, snippets=snippets, plan=plan,
        )
        top = triage.get("top_urls_to_fetch") or []
        urls_to_fetch: list[str] = []
        for entry in top[:MAX_URLS_TO_FETCH]:
            if isinstance(entry, dict):
                u = entry.get("url")
            else:
                u = entry
            if u:
                urls_to_fetch.append(u)

        # Triage-failure fallback: rank Serper snippets by tier and pick the
        # top MAX_URLS_TO_FETCH so we still get content even when the LLM
        # returned an unparseable Triage response.
        if not urls_to_fetch:
            ranked = rank_urls([s.get("link") for s in snippets if s.get("link")])
            urls_to_fetch = [u for u, _t in ranked[:MAX_URLS_TO_FETCH]]

        if not urls_to_fetch:
            result.summary = (
                "I found search results but couldn't pick a usable source. "
                "Try sharing the URL directly."
            )
            result.confidence_overall = "low"
            return

        # Stage 4 — FETCH (parallel)
        import asyncio

        contents = await asyncio.gather(
            *[fetch_webpage(u) for u in urls_to_fetch],
            return_exceptions=True,
        )
        fetched: list[tuple[str, str]] = []
        for u, c in zip(urls_to_fetch, contents, strict=False):
            if isinstance(c, Exception):
                logger.warning("[research] Stage 4 fetch failed for %s: %s", u[:80], c)
                continue
            result.urls_fetched.append(u)
            if c.startswith("BLOCKED:") or not _is_usable(c):
                continue
            self._absorb_fetched_page(u, c, result, append_summary=False)
            fetched.append((u, c))

        if not fetched:
            result.summary = (
                "Found search results but all top URLs were bot-protected "
                "or thin. Try sharing the URL directly so I can fetch it."
            )
            result.missing.append("usable content from the top URLs")
            result.confidence_overall = "low"
            return

        # Stage 5 — EXTRACT (per-page, sequential, gpt-4o for primary)
        extracted = await self._stage_extract(req, fetched=fetched, plan=plan)
        findings = self._parse_findings(extracted)
        missing = sorted(set(extracted.get("missing") or []))

        # Stage 6 — VERIFY (cross-source agreement, no LLM call)
        findings = self._stage_verify(findings)
        result.findings = findings

        # Stage 7 — SYNTHESIZE (gpt-4o builds the structured scout report)
        if findings:
            tactical = await self._stage_synthesize(
                req, findings=findings, missing=missing,
            )
            result.tactical_insights = tactical or {}
            # Set confidence_overall from the synthesized output if present.
            conf = (tactical or {}).get("confidence_overall")
            if conf in ("high", "medium", "low"):
                result.confidence_overall = conf
            else:
                best_tier = min(
                    (s.tier for s in result.sources), default=4,
                )
                result.confidence_overall = "medium" if best_tier <= 2 else "low"
            # Keep `summary` as a readable text rendering of the report so
            # legacy callers (and the research-tool wrapper) still get a
            # text payload. The renderer in `models.ResearchResult` does
            # the structured layout via `to_text_for_agent()`.
            result.summary = result.to_text_for_agent()
        else:
            # No findings — fall back to the raw fetched content so the
            # agent at least has something to read.
            combined_chunks = [
                f"=== {u} ===\n{c[:8000]}" for u, c in fetched
            ]
            result.summary = "\n\n".join(combined_chunks)[:30_000]
            best_tier = min(
                (s.tier for s in result.sources), default=4,
            )
            result.confidence_overall = "medium" if best_tier <= 2 else "low"

        result.missing.extend(m for m in missing if m not in result.missing)

    def _absorb_fetched_page(
        self,
        url: str,
        content: str,
        result: ResearchResult,
        *,
        append_summary: bool = True,
    ) -> None:
        """Append a Source + minimal Finding for a successfully-fetched
        page. When `append_summary` is True, also write the content
        into `result.summary` (used by the URL-hint fast path)."""
        tier = url_tier(url) or 4
        result.sources.append(Source(
            url=url, tier=tier,
            snippet_preview=content[:500],
        ))
        result.findings.append(Finding(
            entity="<source>", metric="page_content",
            value=f"{len(content)} characters",
            source_url=url, source_tier=tier,
            confidence="medium",
        ))
        if append_summary:
            result.summary = content[:30_000]
            result.confidence_overall = "medium" if tier <= 2 else "low"

    # ── Stage 1 — PLAN ──────────────────────────────────────────────────

    async def _stage_plan(
        self,
        req: ResearchRequest,
        *,
        bias_domain: str | None,
    ) -> dict[str, Any]:
        """gpt-4o-mini turns the coach's question into a JSON search plan."""
        user_payload_parts = [f"Coach question: {req.query}"]
        if req.level_hint:
            user_payload_parts.append(f"League hint: {req.level_hint}")
        if bias_domain:
            user_payload_parts.append(
                f"bias_domain: {bias_domain} "
                "(coach's URL was here, prefer this domain)"
            )
        user_payload_parts.append(
            f"\nTrusted TIER_1 domains to prefer in site: queries:\n"
            f"  {', '.join(sorted(TIER_1))}\n"
            f"TIER_2 (use if no T1 match): {', '.join(sorted(TIER_2))}"
        )

        try:
            client = get_client()
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=MAX_TOKENS_PLAN,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": get_plan_prompt()},
                    {"role": "user", "content": "\n".join(user_payload_parts)},
                ],
            )
            await self._log_cost(resp, agent_key="research_plan", req=req)
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as e:
            logger.warning("[research] Stage 1 PLAN failed: %s", e)
            return {}

    # ── Stage 3 — TRIAGE ────────────────────────────────────────────────

    async def _stage_triage(
        self,
        req: ResearchRequest,
        *,
        snippets: list[dict],
        plan: dict,
    ) -> dict[str, Any]:
        """gpt-4o-mini ranks Serper snippets, picks top URLs to fetch."""
        # Pre-tag each snippet with its tier so the LLM can prioritize
        snippets_with_tier: list[dict] = []
        for s in snippets[:30]:  # cap to keep prompt small
            t = url_tier(s.get("link", ""))
            snippets_with_tier.append({
                **s,
                "tier": t if t is not None else "ignored",
            })

        user_payload = (
            "PLAN context:\n"
            f"{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
            "Snippets to triage:\n"
            f"{json.dumps(snippets_with_tier, ensure_ascii=False, indent=2)}"
        )

        try:
            client = get_client()
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=MAX_TOKENS_TRIAGE,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": TRIAGE_PROMPT},
                    {"role": "user", "content": user_payload},
                ],
            )
            await self._log_cost(resp, agent_key="research_triage", req=req)
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as e:
            logger.warning("[research] Stage 3 TRIAGE failed: %s", e)
            return {}

    # ── Stage 5 — EXTRACT (per-page, sequential, gpt-4o primary) ─────

    async def _stage_extract(
        self,
        req: ResearchRequest,
        *,
        fetched: list[tuple[str, str]],
        plan: dict,
    ) -> dict[str, Any]:
        """Per-page structured extraction. Verbatim port of v1
        web_researcher.py:435-570 — primary (highest-tier) source uses
        gpt-4o, secondaries use gpt-4o-mini (different TPM bucket so
        the SYNTHESIZE call later isn't starved). Sequential because
        parallel calls would burn ~26K tokens in <1s and trip tier-1
        rate limits.

        Early-exit kicks in when:
          (a) primary source delivered ≥8 player findings alone, OR
          (b) two sources have ≥5 player findings between them.
        Mirrors v1's break conditions byte-for-byte.
        """
        if not fetched:
            return {"findings": [], "missing": []}

        # Sort by tier — strongest source first (extracted with gpt-4o).
        ordered = sorted(fetched, key=lambda uc: url_tier(uc[0]) or 9)

        merged_findings: list[dict] = []
        merged_missing: set[str] = set()

        team_entities_lower = {
            (e or "").lower()
            for e in (plan.get("entities_to_verify") or [])
        }

        async def _extract_one(
            url: str, content: str, *, use_mini: bool,
        ) -> dict[str, Any]:
            if len(content) > EXTRACT_CHAR_CAP:
                content = content[:EXTRACT_CHAR_CAP]
            user_payload = (
                f"COACH'S QUESTION: {plan.get('league_inferred', '')}\n"
                f"ENTITIES TO VERIFY: "
                f"{json.dumps(plan.get('entities_to_verify', []))}\n"
                f"EXPECTED DATA TYPES: "
                f"{json.dumps(plan.get('expected_data_types', []))}\n\n"
                f"### Source URL: {url}\n{content}"
            )
            page_tier = url_tier(url)
            model = "gpt-4o-mini" if use_mini else "gpt-4o"
            try:
                client = get_client()
                resp = await client.chat.completions.create(
                    model=model,
                    max_tokens=MAX_TOKENS_EXTRACT,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "system",
                            "content": get_extract_prompt(source_tier=page_tier),
                        },
                        {"role": "user", "content": user_payload},
                    ],
                )
                await self._log_cost(
                    resp, agent_key=f"research_extract_{model}", req=req,
                )
                return json.loads(resp.choices[0].message.content or "{}")
            except Exception as e:
                logger.warning(
                    "[research] per-page extract (%s) failed for %s: %s",
                    model, url[:80], e,
                )
                return {"findings": [], "missing": []}

        for idx, (url, content) in enumerate(ordered):
            page_result = await _extract_one(url, content, use_mini=(idx > 0))
            for f in (page_result.get("findings") or []):
                if isinstance(f, dict):
                    merged_findings.append(f)
            for m in (page_result.get("missing") or []):
                if isinstance(m, str):
                    merged_missing.add(m)

            # Early-exit decision: count player-level findings (entities
            # that aren't the team itself, metrics that aren't roster lists).
            player_findings = sum(
                1 for f in merged_findings
                if isinstance(f, dict)
                and (f.get("entity") or "").lower() not in team_entities_lower
                and (f.get("metric") or "").lower() not in {
                    "roster", "key_players", "key players",
                }
            )
            sources_consumed = idx + 1
            should_break = False
            reason = ""
            if sources_consumed == 1 and player_findings >= 8:
                should_break = True
                reason = "primary source delivered rich data alone"
            elif sources_consumed >= 2 and player_findings >= 5:
                should_break = True
                reason = "have multi-source corroboration"
            if should_break and sources_consumed < len(ordered):
                logger.info(
                    "[research] early-exit extract (%s): %d player findings "
                    "from %d sources; skipping %d remaining source(s)",
                    reason, player_findings, sources_consumed,
                    len(ordered) - sources_consumed,
                )
                break

        return {
            "findings": merged_findings,
            "missing": sorted(merged_missing),
        }

    def _parse_findings(self, extracted: dict[str, Any]) -> list[Finding]:
        """Convert raw EXTRACT JSON into Finding dataclasses. Mirrors
        v1 _parse_findings at web_researcher.py:572-589 — silently drops
        malformed rows so a single bad LLM output doesn't poison the
        whole turn."""
        out: list[Finding] = []
        for f in (extracted.get("findings") or []):
            try:
                src_url = (f.get("source_url") or "")
                out.append(Finding(
                    entity=str(f.get("entity") or ""),
                    metric=str(f.get("metric") or ""),
                    value=str(f.get("value") or ""),
                    source_url=src_url,
                    source_tier=url_tier(src_url) or 3,
                    confidence=f.get("confidence") or "medium",
                    cross_source_count=1,
                ))
            except Exception as e:
                logger.debug("[research] skipping malformed finding: %s", e)
        return out

    # ── Stage 6 — VERIFY (cross-source agreement, no LLM) ────────────

    def _stage_verify(self, findings: list[Finding]) -> list[Finding]:
        """Group by (entity, metric, normalized_value) and bump confidence
        when 2+ distinct sources agree. Pure counting — no LLM call.
        Mirrors v1 web_researcher.py:593-616."""
        groups: dict[tuple[str, str, str], set[str]] = {}
        for f in findings:
            key = (
                f.entity.lower().strip(),
                f.metric.lower().strip(),
                _normalize_value(f.value),
            )
            groups.setdefault(key, set()).add(f.source_url)

        for f in findings:
            key = (
                f.entity.lower().strip(),
                f.metric.lower().strip(),
                _normalize_value(f.value),
            )
            count = len(groups.get(key, set()))
            f.cross_source_count = count
            if count >= 2 and f.confidence == "medium":
                f.confidence = "high"
            if count >= 3:
                f.confidence = "high"
        return findings

    # ── Stage 7 — SYNTHESIZE (gpt-4o, structured scout report) ───────

    async def _stage_synthesize(
        self,
        req: ResearchRequest,
        *,
        findings: list[Finding],
        missing: list[str],
    ) -> dict[str, Any]:
        """gpt-4o builds the structured scout report (team_profile,
        differentials, personnel, matchup_prep). Mirrors v1
        web_researcher.py:618-654."""
        findings_payload = [
            {
                "entity": f.entity,
                "metric": f.metric,
                "value": f.value,
                "source_url": f.source_url,
                "source_tier": f.source_tier,
                "confidence": f.confidence,
                "cross_source_count": f.cross_source_count,
            }
            for f in findings
        ]
        user_payload = (
            f"COACH'S ORIGINAL QUESTION: {req.query}\n\n"
            f"VERIFIED FINDINGS:\n"
            f"{json.dumps(findings_payload, ensure_ascii=False, indent=2)}\n\n"
            f"MISSING (asked for but not found in any source):\n"
            f"{json.dumps(missing, ensure_ascii=False)}"
        )
        try:
            client = get_client()
            resp = await client.chat.completions.create(
                model="gpt-4o",
                max_tokens=MAX_TOKENS_SYNTHESIZE,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": get_synthesize_prompt()},
                    {"role": "user", "content": user_payload},
                ],
            )
            await self._log_cost(resp, agent_key="research_synthesize", req=req)
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as e:
            logger.warning("[research] Stage 7 SYNTHESIZE failed: %s", e)
            return {}

    async def _log_cost(self, response, *, agent_key: str, req: ResearchRequest) -> None:
        """Best-effort cost log. Skipped when the researcher was constructed
        without a DB session (CLI / tests)."""
        if self._db is None:
            return
        try:
            await log_response(
                self._db, response,
                user_id=req.user_id, team_id=req.team_id,
                agent_key=agent_key, endpoint="research",
            )
        except Exception as e:
            logger.debug("[research] cost log skipped: %s", e)


def _normalize_value(v: str | None) -> str:
    """Normalize a stat value for cross-source comparison. Strips
    whitespace, drops trailing zeros after the decimal, lowercases.
    Mirrors v1 _normalize_value (web_researcher.py:747-762).

    Examples:
      '85.30'  → '85.3'
      ' 22.5%' → '22.5'
      '1,234'  → '1234'
    """
    if v is None:
        return ""
    s = str(v).strip().lower()
    if "." in s:
        try:
            num = float(s.replace(",", "").replace("%", ""))
            s = f"{num:g}"
        except ValueError:
            pass
    return s


def _domain_of(url: str) -> str | None:
    """Extract the base host (no www., no port) from a URL. Mirrors v1
    `_domain_of` (web_researcher.py:670-676)."""
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host or None
    except Exception:
        return None


__all__ = ["ResearchRequest", "WebResearcher"]
