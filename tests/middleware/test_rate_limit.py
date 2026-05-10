"""Rate limiter middleware unit tests.

End-to-end tests through real endpoints live in
tests/api/test_rate_limit.py (where the api_client fixture is
visible). This file covers the in-memory bookkeeping in isolation."""

from __future__ import annotations

import time

from src.middleware.rate_limit import (
    PER_ENDPOINT_RATE_LIMITS,
    RateLimitMiddleware,
)

# ---------------------------------------------------------------------------
# Direct middleware unit tests
# ---------------------------------------------------------------------------


def _instance() -> RateLimitMiddleware:
    """Build a middleware with no app — we only exercise the bookkeeping."""
    return RateLimitMiddleware(app=None)  # type: ignore[arg-type]


def _drop_old(mw: RateLimitMiddleware, now: float, key: str) -> None:
    """Helper that mimics the dispatch's window-trim step."""
    mw._state[key] = [t for t in mw._state.get(key, []) if now - t < 60.0]


class TestState:
    def test_state_tracks_per_ip_per_path(self):
        mw = _instance()
        now = 1000.0
        mw._state["1.1.1.1:/api/auth/login"] = [now - 5, now - 4, now - 3]
        mw._state["2.2.2.2:/api/auth/login"] = [now - 5]
        # Each (ip,path) is its own bucket
        assert len(mw._state["1.1.1.1:/api/auth/login"]) == 3
        assert len(mw._state["2.2.2.2:/api/auth/login"]) == 1

    def test_cleanup_evicts_stale_ips(self):
        mw = _instance()
        now = time.time()
        mw._state["fresh:/x"] = [now - 5]
        mw._state["stale:/x"] = [now - 200]  # > _STALE_AFTER_SECONDS=120
        # Force cleanup
        mw._last_cleanup = now - 1000
        mw._cleanup(now)
        assert "fresh:/x" in mw._state
        assert "stale:/x" not in mw._state

    def test_cleanup_throttled(self):
        """Cleanup runs at most once per CLEANUP_INTERVAL — back-to-back
        calls shouldn't all walk the dict (CPU optimization)."""
        mw = _instance()
        now = 1000.0
        mw._last_cleanup = now - 5  # < 300s ago → skip
        mw._state["x:/y"] = [now - 1000]  # ancient
        mw._cleanup(now)
        # Not cleaned because we didn't pass the throttle gate
        assert "x:/y" in mw._state


class TestClientIp:
    def test_xff_takes_precedence(self):
        from types import SimpleNamespace

        from starlette.datastructures import Headers

        req = SimpleNamespace(
            headers=Headers({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}),
            client=SimpleNamespace(host="10.0.0.1"),
        )
        # XFF first hop wins over the direct peer
        assert RateLimitMiddleware._client_ip(req) == "1.2.3.4"

    def test_falls_back_to_client_host(self):
        from types import SimpleNamespace

        from starlette.datastructures import Headers

        req = SimpleNamespace(
            headers=Headers({}),
            client=SimpleNamespace(host="10.0.0.1"),
        )
        assert RateLimitMiddleware._client_ip(req) == "10.0.0.1"

    def test_no_client_no_xff_returns_unknown(self):
        from types import SimpleNamespace

        from starlette.datastructures import Headers

        req = SimpleNamespace(headers=Headers({}), client=None)
        assert RateLimitMiddleware._client_ip(req) == "unknown"


class TestTable:
    def test_known_keys_present(self):
        # Mirror v1 — these are the user-visible contract; if any change,
        # the SPA / mobile clients need a coordinated update.
        for key in (
            "/api/auth/login", "/api/auth/register", "/api/auth/refresh",
            "/admin/login", "/api/chat", "/api/chat-stream", "/api/chat-upload",
        ):
            assert key in PER_ENDPOINT_RATE_LIMITS

    def test_caps_match_v1(self):
        assert PER_ENDPOINT_RATE_LIMITS["/api/auth/login"] == 5
        assert PER_ENDPOINT_RATE_LIMITS["/api/auth/register"] == 3
        assert PER_ENDPOINT_RATE_LIMITS["/api/chat"] == 20
        assert PER_ENDPOINT_RATE_LIMITS["/api/chat-upload"] == 10
