"""Tests for OrgContextMiddleware.

Verifies that the middleware reads the org session keys and stamps
`request.state.org_id`/`org_role`/`org_user_id` correctly. Uses a minimal
ASGI app with SessionMiddleware + OrgContextMiddleware so we can exercise
the middleware in isolation without a full FastAPI app.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from src.middleware.org_context import (
    ORG_ACTIVE_ORG_KEY,
    ORG_ACTIVE_ROLE_KEY,
    ORG_SESSION_KEY,
    OrgContextMiddleware,
)

pytestmark = pytest.mark.asyncio


def _make_app() -> FastAPI:
    """Tiny app exposing /set (writes session) and /probe (reads request.state)."""
    app = FastAPI()

    # Same add order as src/main.py: OrgContext FIRST, Session SECOND, so that
    # OrgContext runs INSIDE Session on the request path (after Session resolves).
    app.add_middleware(OrgContextMiddleware)
    app.add_middleware(SessionMiddleware, secret_key="test-secret-key-for-org-context-tests")

    @app.post("/set")
    async def set_session(request: Request):
        body = await request.json()
        if "org_user_id" in body:
            request.session[ORG_SESSION_KEY] = body["org_user_id"]
        if "org_id" in body:
            request.session[ORG_ACTIVE_ORG_KEY] = body["org_id"]
        if "role" in body:
            request.session[ORG_ACTIVE_ROLE_KEY] = body["role"]
        return {"ok": True}

    @app.get("/probe")
    async def probe(request: Request):
        return {
            "org_user_id": request.state.org_user_id,
            "org_id": request.state.org_id,
            "org_role": request.state.org_role,
        }

    return app


class TestOrgContextMiddleware:
    async def test_state_is_none_when_no_session(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/probe")
            assert r.status_code == 200
            assert r.json() == {"org_user_id": None, "org_id": None, "org_role": None}

    async def test_state_reflects_session(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/set",
                json={"org_user_id": 42, "org_id": 7, "role": "org_admin"},
            )
            r = await client.get("/probe")
            assert r.status_code == 200
            assert r.json() == {"org_user_id": 42, "org_id": 7, "org_role": "org_admin"}

    async def test_state_ignores_wrong_types(self):
        # If somehow the session contains a non-int for org_id or non-str for
        # role (e.g., from a tampered cookie), the middleware should default
        # to None rather than passing garbage downstream.
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/set",
                json={"org_user_id": "not-an-int", "org_id": "7", "role": 42},
            )
            r = await client.get("/probe")
            assert r.json() == {"org_user_id": None, "org_id": None, "org_role": None}
