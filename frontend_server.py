"""Frontend static-asset service — separate Railway deploy (optional CDN).

Mirrors the v1.0-flask `frontend_server.py` pattern: a tiny ASGI app that
serves nothing but `frontend/static/*` and a `/health` probe. Built so the
frontend can scale + cache independently of the backend FastAPI app, and
to make a future SPA refactor straightforward (this file becomes the
front door for a built React/Vue bundle later).

Deploy as a separate Railway service from the SAME repo with a custom
start command:

    uvicorn frontend_server:app --host 0.0.0.0 --port $PORT

Until the backend is reconfigured to point `url_for('static', ...)` here
(via the `STATIC_BASE_URL` env var on the backend), this service runs in
parallel: both serve `/static/*`, but the rendered HTML still references
the backend-served path. Cutover is a single env-var flip on the backend.
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles


_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _BASE_DIR / "frontend" / "static"


async def _health(_request):  # pragma: no cover — trivial probe
    return JSONResponse({"status": "ok", "service": "frontend"})


# CORS: allow the backend domain (and any custom domain) to load assets from us.
# The backend's CSP is what actually gates loading; here we just keep CORS open
# for the asset paths so cross-origin <link>/<script>/<img> tags work.
_allowed_origins_env = os.environ.get("CORS_ORIGINS", "").strip()
_allowed_origins: list[str]
if _allowed_origins_env:
    _allowed_origins = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
else:
    # Permissive default — static assets are public anyway.
    _allowed_origins = ["*"]


app = Starlette(
    debug=False,
    routes=[
        Route("/health", _health),
        Route("/healthz", _health),
        Mount(
            "/static",
            StaticFiles(directory=str(_STATIC_DIR)),
            name="static",
        ),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=_allowed_origins,
            allow_methods=["GET", "HEAD", "OPTIONS"],
            allow_headers=["*"],
            max_age=86400,
        ),
    ],
)
