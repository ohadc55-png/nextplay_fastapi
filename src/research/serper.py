"""Serper search API wrapper — async port of v1 `_serper_one`.

Why our own thin wrapper instead of crewai_tools' SerperDevTool?
We want clean structured snippets (title / link / snippet) for the
Triage stage to rank. SerperDevTool's `.run()` returns formatted text,
which would force us to regex-parse for URLs — fragile.

Falls back gracefully:
  - missing API key → returns []
  - non-200 from Serper → returns []
  - any exception → returns []
The Plan stage is expected to handle empty search results by emitting
a "share a link" message rather than crashing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)


def _serper_sync(query: str, *, num: int = 10, timeout: int = 8) -> list[dict[str, Any]]:
    """Single Serper request. Returns a list of {title, snippet, link} dicts.
    Empty list on any failure — never raises."""
    if not query or not query.strip():
        return []
    api_key = (settings.SERPER_API_KEY or "").strip()
    if not api_key:
        logger.debug("[serper] SERPER_API_KEY missing — search disabled")
        return []

    import requests

    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": int(num)},
            timeout=int(timeout),
        )
    except Exception as e:
        logger.warning("[serper] HTTP error for %r: %s", query[:60], e)
        return []
    if not r.ok:
        logger.warning("[serper] non-200 for %r: %s", query[:60], r.status_code)
        return []
    try:
        data = r.json()
    except Exception as e:
        logger.warning("[serper] bad JSON for %r: %s", query[:60], e)
        return []

    organic = data.get("organic") or []
    out: list[dict[str, Any]] = []
    for row in organic[:num]:
        out.append({
            "title": row.get("title", ""),
            "snippet": row.get("snippet", ""),
            "link": row.get("link", ""),
        })
    return out


async def serper_search(query: str, *, num: int = 10, timeout: int = 8) -> list[dict[str, Any]]:
    """Async wrapper around `_serper_sync`. Off-thread so the event
    loop keeps spinning during the 8s HTTP roundtrip."""
    return await asyncio.to_thread(_serper_sync, query, num=num, timeout=timeout)


async def serper_batch(queries: list[str], *, num: int = 10) -> list[dict[str, Any]]:
    """Run multiple Serper queries concurrently; aggregate snippets.

    Each result row is tagged with `query_origin` so Triage can see
    which query produced which URL. Failures from individual queries
    don't poison the batch.
    """
    if not queries:
        return []
    # Cap concurrency — Serper rate limits + we don't need 50 in flight.
    semaphore = asyncio.Semaphore(5)

    async def _one(q: str) -> list[dict[str, Any]]:
        async with semaphore:
            rows = await serper_search(q, num=num)
        return [{**r, "query_origin": q} for r in rows]

    results_per_query = await asyncio.gather(
        *[_one(q) for q in queries], return_exceptions=True,
    )
    aggregated: list[dict[str, Any]] = []
    for q, rows in zip(queries, results_per_query, strict=False):
        if isinstance(rows, Exception):
            logger.warning("[serper] batch query failed for %r: %s", q[:60], rows)
            continue
        aggregated.extend(rows)
    return aggregated


__all__ = ["serper_batch", "serper_search"]
