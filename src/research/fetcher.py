"""4-layer web fetcher — Jina default → Jina browser → Scrape → Playwright.

Async port of `backend/tools.py` `fetch_webpage` (~v1 lines 165-282) plus
the heuristic helpers (`_looks_like_bot_gate`, `_looks_like_404`,
`_looks_like_nav_only`).

All four layers are sync HTTP calls; we wrap each layer in
`asyncio.to_thread` so the FastAPI event loop keeps spinning while a
35-second Jina request is in flight. Playwright is env-gated by
`ENABLE_PLAYWRIGHT_FALLBACK` — it ships installed locally but Railway's
Nixpacks image lacks Chromium, so the v1 behavior is to skip layer 4 in
prod.

Returns content (markdown-ish) capped at MAX_CONTENT_CHARS, or a BLOCKED
explanation string when every layer returns thin/blocked/nav-only content.
"""

from __future__ import annotations

import asyncio
import logging

from src.core.config import settings

logger = logging.getLogger(__name__)


MAX_CONTENT_CHARS = 65000  # matches v1 (line 212)
JINA_TIMEOUT = 35

# Heuristic phrases pulled from v1 backend/tools.py:_looks_like_bot_gate.
# Single-string subset that's enough to bail when content is junk.
_BOT_GATE_MARKERS = (
    "checking your browser",
    "verify you are human",
    "cloudflare",
    "captcha",
    "request unsuccessful. incapsula incident",
    "datadome",
    "access denied",
    "you don't have permission to access",
)

_404_MARKERS = (
    "404 not found",
    "page not found",
    "this page does not exist",
    "we can't find the page",
)

# Words that show up only in nav/menu HTML — when most of the response is
# these, we're probably looking at a sidebar, not real content.
_NAV_KEYWORDS = (
    "home", "about", "contact", "privacy", "terms",
    "log in", "login", "sign up", "sign in",
    "menu", "search", "subscribe",
)


def _looks_like_bot_gate(text: str) -> bool:
    if not text:
        return True
    low = text.lower()
    return any(m in low for m in _BOT_GATE_MARKERS)


def _looks_like_404(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _404_MARKERS)


def _looks_like_nav_only(text: str) -> bool:
    if not text or len(text) < 200:
        return True
    low = text.lower()
    if len(low) > 2000:
        return False
    nav_hits = sum(1 for kw in _NAV_KEYWORDS if kw in low)
    return nav_hits >= 6


def _is_usable(text: str) -> bool:
    if _looks_like_bot_gate(text):
        return False
    if _looks_like_404(text):
        return False
    if _looks_like_nav_only(text):
        return False
    return True


# ---------------------------------------------------------------------------
# Synchronous fetchers — wrapped in asyncio.to_thread by fetch_webpage()
# ---------------------------------------------------------------------------


def _jina_get_sync(url: str, *, browser_engine: bool = False) -> str:
    """Single Jina Reader request. Returns body text (possibly empty)."""
    import requests

    headers = {"Accept": "text/plain"}
    if settings.JINA_API_KEY:
        headers["Authorization"] = f"Bearer {settings.JINA_API_KEY}"
    if browser_engine:
        headers["X-Engine"] = "browser"
        headers["X-Timeout"] = "30"
        headers["X-With-Iframe"] = "true"
    try:
        r = requests.get(
            f"https://r.jina.ai/{url}", headers=headers, timeout=JINA_TIMEOUT,
        )
    except Exception as e:
        logger.warning("[fetcher] Jina (%s) error for %s: %s",
                       "browser" if browser_engine else "default", url[:80], e)
        return ""
    if not r.ok:
        return ""
    return r.text or ""


def _scrape_tool_sync(url: str) -> str:
    """Layer 3: CrewAI's ScrapeWebsiteTool. Basic HTTP GET, no JS.
    Returns "" if CrewAI's tools aren't available (light dev installs)."""
    try:
        from crewai_tools import ScrapeWebsiteTool

        tool = ScrapeWebsiteTool()
        result = tool.run(website_url=url)
        return result if isinstance(result, str) else str(result or "")
    except Exception as e:
        logger.warning("[fetcher] ScrapeWebsiteTool error for %s: %s", url[:80], e)
        return ""


def _playwright_sync(url: str, *, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """Layer 4: Playwright headless Chromium. Env-gated.
    Skipped entirely when `ENABLE_PLAYWRIGHT_FALLBACK` is "0" / falsy
    (Railway prod, where Chromium isn't in the build image)."""
    flag = (settings.ENABLE_PLAYWRIGHT_FALLBACK or "").strip().lower()
    if flag in ("0", "false", "no", ""):
        return ""
    try:
        # Lazy import — keeps the codebase runnable when playwright isn't
        # installed (matches v1's behavior).
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(url, timeout=30000, wait_until="networkidle")
                content = page.evaluate("() => document.body.innerText")
            finally:
                browser.close()
        return (content or "")[:max_chars]
    except Exception as e:
        logger.warning("[fetcher] Playwright error for %s: %s", url[:80], e)
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _blocked_message(url: str) -> str:
    """The exact BLOCKED signal v1 returns. The Scout prompt parses this
    string to trigger a Serper fallback — keep it byte-for-byte."""
    return (
        f"BLOCKED: The page at {url} is either bot-protected (Cloudflare / "
        f"DataDome) or renders its real content via JavaScript that our scraper "
        f"cannot execute. The only readable text was navigation/menu boilerplate. "
        f"REQUIRED NEXT STEP: you MUST now call 'search_the_internet_with_serper' "
        f"with 2-3 focused queries combining the team/player name + keywords like "
        f"'statistics', 'roster', 'recent games', both in English and the coach's "
        f"language. Build your answer from search snippets. Do NOT respond to the "
        f"coach with 'DATA NOT AVAILABLE' until you have tried Serper."
    )


async def fetch_webpage(url: str) -> str:
    """4-layer fetch chain. Returns content string or a BLOCKED notice.

    Async wrapper: each layer's sync HTTP call is dispatched via
    `asyncio.to_thread` so the event loop keeps serving other coaches
    while one fetch is in flight.
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: Invalid URL. Must start with http:// or https://"

    # Layer 1 — default Jina Reader
    text = await asyncio.to_thread(_jina_get_sync, url, browser_engine=False)
    if text and _is_usable(text):
        return text[:MAX_CONTENT_CHARS]

    # Layer 2 — Jina browser engine
    text = await asyncio.to_thread(_jina_get_sync, url, browser_engine=True)
    if text and _is_usable(text):
        return text[:MAX_CONTENT_CHARS]

    # Layer 3 — Scrape tool (basic HTTP)
    text = await asyncio.to_thread(_scrape_tool_sync, url)
    if text and _is_usable(text):
        return text[:MAX_CONTENT_CHARS]

    # Layer 4 — Playwright (env-gated)
    text = await asyncio.to_thread(_playwright_sync, url)
    if text and _is_usable(text):
        return text[:MAX_CONTENT_CHARS]

    return _blocked_message(url)


__all__ = [
    "MAX_CONTENT_CHARS",
    "_blocked_message",
    "_is_usable",  # exported for tests
    "_looks_like_404",
    "_looks_like_bot_gate",
    "_looks_like_nav_only",
    "fetch_webpage",
]
