"""Hourly cache for research results — re-keyed by tenant.

THIS FILE FIXES A SECURITY BUG IN v1.0-flask.

In v1, the cache key was `(query, level_hint, url_hint, hour_bucket)`
([backend/research/web_researcher.py:684](../../basketball_coach_ai/backend/research/web_researcher.py#L684)).
That meant if Coach A asked "scout Maccabi Tel Aviv" at 14:32 and Coach B
asked the same question at 14:55, B got A's cached result. If A's prior
chat context shaped what was cached (e.g. a query that pulled coach-private
context into the LLM-driven scout report), B got A's data — a tenant leak.

The fix mandated by master prompt §3.1 is to re-key by tenant:
  (user_id, team_id, query, level_hint, url_hint, hour_bucket)

So even when the query string and hour match exactly, two different
coaches always get separate cache entries.

Storage: in-process dict with FIFO eviction at 256 entries. Same as v1 —
Redis-backed multi-process eviction is a Phase 7+ task.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.research.models import ResearchResult


def current_hour_bucket() -> int:
    """Integer that increments once per hour. Used for cache invalidation."""
    return int(time.time() // 3600)


# Key shape — see module docstring for why each field matters.
_CacheKey = tuple[int, int | None, str, str, str, int]
#                user team    q   l    u   hour

_RESULT_CACHE: dict[_CacheKey, ResearchResult] = {}
_CACHE_MAX = 256


def _build_key(
    *,
    user_id: int,
    team_id: int | None,
    query: str,
    level_hint: str | None,
    url_hint: str | None,
    hour: int,
) -> _CacheKey:
    """Normalize the inputs into a deterministic cache key.
    Mirrors the v1 q_key/l_key/u_key normalization (lowercased + clamped)."""
    q_key = (query or "").strip().lower()[:600]
    l_key = (level_hint or "").strip().lower()[:60]
    u_key = (url_hint or "").strip()[:300]
    return (user_id, team_id, q_key, l_key, u_key, hour)


def lookup(
    *,
    user_id: int,
    team_id: int | None,
    query: str,
    level_hint: str | None,
    url_hint: str | None,
    hour: int | None = None,
) -> ResearchResult | None:
    """Return the cached result for this tenant + query in this hour, or None.

    Critically: a different `user_id` ALWAYS produces a different key, so
    two coaches asking the same question never share a cache entry."""
    if hour is None:
        hour = current_hour_bucket()
    key = _build_key(
        user_id=user_id, team_id=team_id, query=query,
        level_hint=level_hint, url_hint=url_hint, hour=hour,
    )
    return _RESULT_CACHE.get(key)


def store(
    *,
    user_id: int,
    team_id: int | None,
    query: str,
    level_hint: str | None,
    url_hint: str | None,
    result: ResearchResult,
    hour: int | None = None,
) -> None:
    """Insert with FIFO eviction at MAX size. Mirrors v1 (line 693-700)."""
    if hour is None:
        hour = current_hour_bucket()
    key = _build_key(
        user_id=user_id, team_id=team_id, query=query,
        level_hint=level_hint, url_hint=url_hint, hour=hour,
    )
    if len(_RESULT_CACHE) >= _CACHE_MAX:
        oldest = next(iter(_RESULT_CACHE))
        _RESULT_CACHE.pop(oldest, None)
    _RESULT_CACHE[key] = result


def clear() -> None:
    """Wipe the cache. Used by tests."""
    _RESULT_CACHE.clear()


def size() -> int:
    """Number of cached entries. Used by tests."""
    return len(_RESULT_CACHE)


__all__ = ["clear", "current_hour_bucket", "lookup", "size", "store"]
