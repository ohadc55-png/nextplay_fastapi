"""Rate-limit middleware end-to-end tests through real endpoints.

Pure middleware unit tests live in tests/middleware/test_rate_limit.py.
This file specifically exercises the table → 429 flow through the
HTTP stack, which needs the api_client + auth fixtures.

The default api_client fixture turns the limiter OFF (so every other
test isn't choked by shared in-memory state). These tests turn it
back ON via a contextvar override INSIDE each test (so it wins over
the fixture's `patch.object`). The middleware's per-(ip,path) bucket
gets wiped between tests so old state from previous tests doesn't
leak."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from httpx import AsyncClient

from src.core.config import settings
from src.main import app
from src.middleware.rate_limit import RateLimitMiddleware


def _reset_limiter_state() -> None:
    """Walk the middleware chain to find RateLimitMiddleware and clear
    its per-(ip,path) bucket so successive tests start fresh."""
    seen = app
    while hasattr(seen, "app"):
        if isinstance(seen, RateLimitMiddleware):
            seen._state.clear()
            seen._last_cleanup = 0.0
            return
        seen = seen.app


@contextmanager
def _limiter_enabled():
    """Override the api_client fixture's RATE_LIMIT_ENABLED=False patch
    with a fresh, ON state. patch.object stacks last-in-first-out so
    this is the active value while the with block runs."""
    _reset_limiter_state()
    with patch.object(settings, "RATE_LIMIT_ENABLED", True):
        yield


class TestLoginRateLimit:
    async def test_blocks_at_cap(self, api_client: AsyncClient):
        """Six wrong-password attempts in a fresh window — the 6th
        returns 429, NOT 401. The earlier 5 return their normal
        validation code (here 401 / 422 depending on the route)."""
        with _limiter_enabled():
            for i in range(5):
                r = await api_client.post(
                    "/api/auth/login",
                    json={"email": f"x{i}@y.com", "password": "wrong"},
                )
                assert r.status_code != 429, f"hit 429 too early on attempt {i + 1}"

            r = await api_client.post(
                "/api/auth/login",
                json={"email": "x6@y.com", "password": "wrong"},
            )
            assert r.status_code == 429
            assert "wait a minute" in r.json()["error"].lower()

    async def test_get_requests_not_limited(self, api_client: AsyncClient):
        """Spam GET /healthz — never gets capped because the rate table
        only covers state-changing endpoints."""
        with _limiter_enabled():
            for _ in range(50):
                r = await api_client.get("/healthz")
            assert r.status_code == 200

    async def test_endpoint_not_in_table_not_limited(
        self, api_client: AsyncClient
    ):
        """POST /api/feedback isn't in the table — no cap, no 429."""
        with _limiter_enabled():
            for _ in range(30):
                r = await api_client.post(
                    "/api/feedback",
                    json={"rating": 1, "comment": "hi"},
                )
                assert r.status_code != 429
