"""Per-IP, per-endpoint rate-limit middleware.

Phase 7 batch 5. Async port of the v1 `_PER_ENDPOINT_RATE_LIMITS` table
+ pre-request hook in `flask_app.py:271-299`. Same per-process limit
semantics — Redis backing is a future improvement (documented in
MIGRATION_TODO).

Why our own middleware instead of slowapi?
  - slowapi adds a 4-line decorator per endpoint; we want a single
    middleware that knows the table.
  - The cleanup logic (drop IPs idle for 2 min) is hand-rolled in v1
    and we mirror it.

Behavior:
  - Only applies to POST/PUT/PATCH/DELETE — GETs are not in the table.
  - IP comes from `X-Forwarded-For` (first hop) or `request.client.host`.
    Trust-the-edge model — Railway puts a load balancer in front so
    XFF is reliable. Behind a non-trusted proxy, attacker control of
    XFF would let them bypass; we accept that until we deploy on a
    different topology.
  - 429 with `{"error": "Too many attempts. Please wait a minute."}` —
    same body shape as v1 so the SPA's existing error parser works.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from src.core.config import settings

logger = logging.getLogger(__name__)


# Limits in requests-per-minute. Mirrors v1 flask_app.py:260-270.
PER_ENDPOINT_RATE_LIMITS: dict[str, int] = {
    # Auth — brute-force protection
    "/api/auth/login": 5,
    "/api/auth/register": 3,
    "/api/auth/refresh": 10,
    "/admin/login": 5,
    # LLM-cost endpoints — DoS-against-our-API-bill protection
    "/api/chat": 20,
    "/api/chat-stream": 20,
    "/api/chat-upload": 10,
}

# Window in seconds.
_WINDOW_SECONDS = 60.0
# Stale-IP cleanup runs at most once per this many seconds.
_CLEANUP_INTERVAL = 300.0
# IPs idle longer than this get evicted from the table.
_STALE_AFTER_SECONDS = 120.0


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP per-endpoint sliding-window counter."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        # `_state` is `dict[(ip, path), list[float_timestamps]]`. Each
        # request appends `now` and we drop entries older than the
        # window. Memory is bounded by the cleanup pass.
        self._state: dict[str, list[float]] = {}
        self._last_cleanup = 0.0

    @staticmethod
    def _client_ip(request: Request) -> str:
        # Trust X-Forwarded-For first hop when behind a proxy. Falls back
        # to `request.client.host` (the direct peer) for direct hits.
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return first
        client = request.client
        return client.host if client else "unknown"

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup <= _CLEANUP_INTERVAL:
            return
        stale = [
            k for k, ts_list in self._state.items()
            if not ts_list or now - ts_list[-1] > _STALE_AFTER_SECONDS
        ]
        for k in stale:
            self._state.pop(k, None)
        self._last_cleanup = now

    async def dispatch(self, request: Request, call_next) -> Response:
        # Test-mode short-circuit. Reads at request time (not constructor)
        # so a test can flip the flag back on for its own e2e check.
        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)

        path = request.url.path
        cap = PER_ENDPOINT_RATE_LIMITS.get(path)
        # GET / OPTIONS / HEAD are never gated — only state-changing methods.
        if cap is None or request.method.upper() in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        now = time.time()
        self._cleanup(now)

        ip = self._client_ip(request)
        key = f"{ip}:{path}"

        # Drop entries outside the sliding window
        entries = [t for t in self._state.get(key, []) if now - t < _WINDOW_SECONDS]
        if len(entries) >= cap:
            # 429. Body shape matches v1 so existing SPA parsers work.
            return JSONResponse(
                status_code=429,
                content={"error": "Too many attempts. Please wait a minute."},
            )
        entries.append(now)
        self._state[key] = entries

        return await call_next(request)


__all__ = ["PER_ENDPOINT_RATE_LIMITS", "RateLimitMiddleware"]
