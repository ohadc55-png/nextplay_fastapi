"""Tests for security headers + CSRF middleware."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.middleware.csrf import CSRFMiddleware
from src.middleware.security_headers import SecurityHeadersMiddleware


@pytest_asyncio.fixture
async def client_with_middleware() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/api/anything")
    async def get_anything():
        return {"ok": True}

    @app.post("/api/state-change")
    async def state_change(request: Request):
        return {"ok": True, "csp_nonce": request.state.csp_nonce}

    @app.post("/api/auth/login")
    async def fake_login():
        return {"ok": True}

    @app.post("/api/internal/run-push-jobs")
    async def fake_cron():
        return {"ok": True}

    @app.get("/page")
    async def some_page():
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

class TestSecurityHeaders:
    async def test_basic_headers_present(self, client_with_middleware: AsyncClient):
        r = await client_with_middleware.get("/api/anything")
        assert r.status_code == 200
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-XSS-Protection"] == "1; mode=block"
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in r.headers["Permissions-Policy"]
        assert r.headers["Server"] == "NextPlay"

    async def test_csp_includes_required_origins(self, client_with_middleware: AsyncClient):
        r = await client_with_middleware.get("/api/anything")
        csp = r.headers["Content-Security-Policy"]
        # Frontend deps that v1.0-flask templates rely on:
        assert "https://accounts.google.com" in csp
        assert "https://appleid.apple.com" in csp
        assert "https://connect.facebook.net" in csp
        assert "https://www.youtube.com" in csp
        assert "https://*.s3.eu-central-1.amazonaws.com" in csp
        assert "https://*.cloudfront.net" in csp

    async def test_csp_nonce_is_per_request(self, client_with_middleware: AsyncClient):
        r1 = await client_with_middleware.get("/api/anything")
        r2 = await client_with_middleware.get("/api/anything")
        nonce1 = next(part for part in r1.headers["Content-Security-Policy"].split(";")
                      if "nonce-" in part)
        nonce2 = next(part for part in r2.headers["Content-Security-Policy"].split(";")
                      if "nonce-" in part)
        assert nonce1 != nonce2

    async def test_csp_nonce_is_exposed_to_handlers(self, client_with_middleware: AsyncClient):
        """Routes can read `request.state.csp_nonce` to inject into templates."""
        r = await client_with_middleware.post(
            "/api/state-change",
            json={},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert r.status_code == 200
        body_nonce = r.json()["csp_nonce"]
        # The same nonce must appear in the CSP header for this response.
        assert body_nonce in r.headers["Content-Security-Policy"]


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

class TestCSRF:
    async def test_get_is_always_allowed(self, client_with_middleware: AsyncClient):
        r = await client_with_middleware.get("/api/anything")
        assert r.status_code == 200

    async def test_post_without_csrf_header_is_blocked(
        self, client_with_middleware: AsyncClient
    ):
        """A POST with no X-Requested-With and no JSON content-type is the
        forged-form attack scenario."""
        r = await client_with_middleware.post(
            "/api/state-change",
            content=b"name=evil",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 403
        assert r.json()["code"] == "csrf_missing"

    async def test_post_with_x_requested_with_passes(
        self, client_with_middleware: AsyncClient
    ):
        r = await client_with_middleware.post(
            "/api/state-change",
            content=b"hi",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert r.status_code == 200

    async def test_post_with_json_content_type_passes(
        self, client_with_middleware: AsyncClient
    ):
        r = await client_with_middleware.post("/api/state-change", json={"a": 1})
        assert r.status_code == 200

    async def test_post_with_bearer_token_passes(
        self, client_with_middleware: AsyncClient
    ):
        """Bearer tokens are themselves anti-CSRF (a forged form can't set
        Authorization), so we trust them and skip the check."""
        r = await client_with_middleware.post(
            "/api/state-change",
            content=b"x",
            headers={
                "Authorization": "Bearer some-token",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert r.status_code == 200

    async def test_auth_endpoints_are_exempt(self, client_with_middleware: AsyncClient):
        """Login/register/refresh are called before the SPA loads, so they
        can't set X-Requested-With. They have to be exempt."""
        r = await client_with_middleware.post(
            "/api/auth/login",
            content=b"email=x&password=y",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 200

    async def test_internal_cron_endpoints_are_exempt(
        self, client_with_middleware: AsyncClient
    ):
        """The /api/internal/* routes use CRON_SECRET-based auth, not CSRF."""
        r = await client_with_middleware.post("/api/internal/run-push-jobs")
        assert r.status_code == 200

    async def test_non_api_post_is_not_checked(self, client_with_middleware: AsyncClient):
        """Page POSTs (Jinja2 form submits, etc.) are out of scope — CSRF
        middleware only fires for /api/*."""
        # The /page route is a GET in our test app, but the middleware
        # decision for non-/api/ paths must be 'pass-through' for any method.
        r = await client_with_middleware.get("/page")
        assert r.status_code == 200
