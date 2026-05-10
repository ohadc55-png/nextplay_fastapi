"""Domain trust ranking for the Research Agent — OPEN-SEARCH MODEL.

Verbatim port of `backend/research/source_authority.py` from v1.0-flask.

Tier 1 = near-perfect trust (use first, cite confidently)
Tier 2 = solid secondary (use if Tier 1 didn't have it)
Tier 3 = weak but allowed (used to be 'last resort')
Tier 4 = unknown/uncategorized — accepted but the LLM is warned to be extra
         careful (likely news/blog/prose, not stat tables).
None  = BLACKLISTED (social, video, junk) — drop entirely.
"""

from __future__ import annotations

from urllib.parse import urlparse

TIER_1: frozenset[str] = frozenset({
    "sports-reference.com",
    "basketball-reference.com",
    "euroleaguebasketball.net",
    "fiba.basketball",
    "nba.com",
    "wnba.com",
    "ncaa.com",
    "d3hoops.com",
    "kenpom.com",
    "barttorvik.com",
})

TIER_2: frozenset[str] = frozenset({
    "espn.com",
    "basketnews.com",
    "eurobasket.com",
    "maxpreps.com",
    "247sports.com",
    "cbssports.com",
    "sofascore.com",
    "flashscore.com",
    "synergysports.com",
    "hudl.com",
})

TIER_3: frozenset[str] = frozenset({
    "rivals.com",
    "proballers.com",
    "wikipedia.org",
    "ballislife.com",
})

BLACKLIST: frozenset[str] = frozenset({
    "reddit.com",
    "pinterest.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "quora.com",
    "linkedin.com",
})


def _normalize_host(url_or_host: str) -> str:
    host = url_or_host
    if "://" in host:
        host = urlparse(host).netloc or ""
    host = host.split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _matches(host: str, domain_set: frozenset[str]) -> bool:
    for d in domain_set:
        if host == d or host.endswith("." + d):
            return True
    return False


def url_tier(url: str) -> int | None:
    """Return 1/2/3/4 for accepted domains, None for BLACKLIST.
    Open-search: unknown domains return 4 (not None) — still fetchable
    but the LLM gets a tier-4 warning."""
    if not url:
        return None
    host = _normalize_host(url)
    if _matches(host, BLACKLIST):
        return None
    if _matches(host, TIER_1):
        return 1
    if _matches(host, TIER_2):
        return 2
    if _matches(host, TIER_3):
        return 3
    return 4


def rank_urls(urls: list[str], limit: int = 5) -> list[tuple[str, int]]:
    """Sort URLs by tier (1 best, 4 worst), drop BLACKLIST, dedupe."""
    seen: set[str] = set()
    out: list[tuple[str, int]] = []
    for u in urls:
        if not u or u in seen:
            continue
        seen.add(u)
        t = url_tier(u)
        if t is None:
            continue
        out.append((u, t))
    out.sort(key=lambda x: x[1])
    return out[:limit]


__all__ = [
    "BLACKLIST",
    "TIER_1",
    "TIER_2",
    "TIER_3",
    "rank_urls",
    "url_tier",
]
