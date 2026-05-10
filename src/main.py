"""FastAPI app entry point.

Phase 0: empty app with health check, CORS, Sentry, and a lifespan hook that
verifies DB connectivity. Routers, middleware, and services are mounted in
later phases.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from src.api import admin as admin_router
from src.api import admin_emails as admin_emails_router
from src.api import admin_orgs as admin_orgs_router
from src.api import admin_pages as admin_pages_router
from src.api import admin_tasks as admin_tasks_router
from src.api import auth as auth_router
from src.api import chat as chat_router
from src.api import coach as coach_router
from src.api import composite as composite_router
from src.api import email_auth as email_auth_router
from src.api import notebook as notebook_router
from src.api import oauth as oauth_router
from src.api import onboarding as onboarding_router
from src.api import org as org_router
from src.api import org_pages as org_pages_router
from src.api import pages as pages_router
from src.api import players as players_router
from src.api import plays as plays_router
from src.api import push as push_router
from src.api import scouting as scouting_router
from src.api import sessions as sessions_router
from src.api import teams as teams_router
from src.api import tracking as tracking_router
from src.api import uploads as uploads_router
from src.core.config import settings
from src.core.database import AsyncSessionLocal, engine
from src.core.exceptions import AppError
from src.middleware.csrf import CSRFMiddleware
from src.middleware.org_context import OrgContextMiddleware
from src.middleware.rate_limit import RateLimitMiddleware
from src.middleware.security_headers import SecurityHeadersMiddleware

logger = logging.getLogger("nextplay")
logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))


# ---------------------------------------------------------------------------
# Sentry — initialize before app creation so it captures startup errors too.
# ---------------------------------------------------------------------------

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        integrations=[FastApiIntegration(), StarletteIntegration()],
        environment=settings.RAILWAY_ENVIRONMENT or "development",
        traces_sample_rate=0.1 if settings.is_production else 0.0,
        send_default_pii=False,
    )
    logger.info("Sentry initialized for environment=%s", settings.RAILWAY_ENVIRONMENT or "development")


# ---------------------------------------------------------------------------
# Lifespan — run startup/shutdown hooks.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup: verify DB connectivity early so we fail fast on misconfig.
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Database connection verified (%s)", _scrub_db_url(settings.database_url_async))
    except Exception as exc:
        logger.error("Database connectivity check failed at startup: %s", exc)
        # Don't raise — let the app boot so /healthz can report degraded state.

    # ChromaDB knowledge base — best-effort. Empty collection is fine
    # (the tool wrapper degrades to "no matches"); a missing persist
    # dir or a chromadb import error logs and continues. Cold-start
    # cost is the persistent client opening a SQLite file on disk.
    try:
        from src.crew.knowledge_base import get_kb

        kb = get_kb()
        n = await kb.count()
        logger.info(
            "Knowledge base ready (%s docs, persist=%s)",
            n, settings.CHROMA_PERSIST_DIR,
        )
    except Exception as exc:
        logger.warning("Knowledge base init failed: %s", exc)

    yield

    # Shutdown
    await engine.dispose()
    logger.info("Engine disposed; shutdown complete.")


def _scrub_db_url(url: str) -> str:
    """Redact credentials before logging the DB URL."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            _creds, host = rest.rsplit("@", 1)
            return f"{scheme}://***@{host}"
    return url


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="NEXTPLAY",
    description="AI basketball coaching assistant — FastAPI migration of Flask app v1.0-flask.",
    version="2.0.0-alpha",
    lifespan=lifespan,
)


# Middleware order matters — Starlette runs them in REVERSE-add order, so the
# LAST add wraps the OTHERS innermost. We want:
#   request → CORS → SecurityHeaders → CSRF → SessionMiddleware → router
# CORS first (needs to run on OPTIONS preflights regardless of CSRF).
# SecurityHeaders must wrap responses to add the headers.
# CSRF must run before the route handler.
# SessionMiddleware is required by Authlib OAuth (state storage).

# OrgContextMiddleware must run AFTER SessionMiddleware on the request path so
# it can read `request.session`. Starlette wraps in REVERSE-add order, so
# adding it BEFORE SessionMiddleware (here) makes it INNERMOST in the stack
# = it runs LAST before the route handler, i.e., AFTER SessionMiddleware.
app.add_middleware(OrgContextMiddleware)

# Authlib's OAuth client needs Starlette's SessionMiddleware to round-trip
# the OAuth `state` between `/auth/<provider>` and `/<provider>/callback`.
# A long random secret is used so a forged session cookie can't impersonate
# a redirect.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET_KEY or settings.JWT_SECRET_KEY or "dev-only-do-not-use",
    same_site="lax",
    https_only=settings.is_production,
)

app.add_middleware(CSRFMiddleware)
# Rate limiter MUST run before CSRF so a flood of bad CSRF requests
# still gets capped. Starlette wraps in REVERSE-add order, so adding
# RateLimitMiddleware AFTER CSRFMiddleware makes RateLimit run FIRST.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Global exception handler — maps AppError tree to JSON responses.
# ---------------------------------------------------------------------------

@app.exception_handler(AppError)
async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code},
    )


# ---------------------------------------------------------------------------
# Health check (Phase 0 verification gate)
# ---------------------------------------------------------------------------

@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    db_ok = True
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    # KB readiness — "ready" if the persistent client connects (even
    # with 0 documents); "empty" specifically calls out an empty
    # collection so deploys notice when ingestion hasn't run yet.
    chroma_status = "ready"
    try:
        from src.crew.knowledge_base import get_kb

        n = await get_kb().count()
        chroma_status = "ready" if n > 0 else "empty"
    except Exception:
        chroma_status = "unavailable"

    return {
        "status": "ok" if db_ok else "degraded",
        "db": "connected" if db_ok else "disconnected",
        "chroma": chroma_status,
        "version": app.version,
        "environment": settings.RAILWAY_ENVIRONMENT or "local",
    }


# ---------------------------------------------------------------------------
# Routers (Phase 3 — auth)
# ---------------------------------------------------------------------------

app.include_router(auth_router.router)
app.include_router(email_auth_router.router)
app.include_router(oauth_router.router)

# ---------------------------------------------------------------------------
# Routers (Phase 4 — domain endpoints, batch by batch)
# ---------------------------------------------------------------------------

app.include_router(tracking_router.router)
app.include_router(push_router.router)
app.include_router(admin_router.router)
app.include_router(admin_tasks_router.router)
app.include_router(admin_emails_router.router)
app.include_router(admin_orgs_router.router)
app.include_router(admin_pages_router.router)
app.include_router(org_router.router)
app.include_router(org_pages_router.router)
app.include_router(notebook_router.router)
app.include_router(onboarding_router.router)
app.include_router(plays_router.router)
app.include_router(scouting_router.router)
app.include_router(coach_router.router)
app.include_router(teams_router.router)
app.include_router(players_router.router)
app.include_router(composite_router.router)
app.include_router(sessions_router.router)
app.include_router(uploads_router.router)

# ---------------------------------------------------------------------------
# Phase 5 — AI stack (in progress; chat now uses direct OpenAI, agents +
# RAG + research land in subsequent batches)
# ---------------------------------------------------------------------------
app.include_router(chat_router.router)

# ---------------------------------------------------------------------------
# Phase 8 — Frontend integration: static files, service workers, page routes
# ---------------------------------------------------------------------------

_FRONTEND_STATIC = "frontend/static"


# Service workers MUST be served from the site root (not /static/) so the
# browser grants them a `/` scope. Cache-Control: no-cache forces the
# browser to revalidate the SW on every page load — without it, SW
# updates can lag by hours. Mirrors v1 frontend/routes.py:15-44.

@app.get("/sw.js", tags=["frontend"], include_in_schema=False)
async def main_service_worker() -> FileResponse:
    """Main SW — combines uploads + offline cache + push handlers."""
    return FileResponse(
        os.path.join(_FRONTEND_STATIC, "sw.js"),
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache, must-revalidate",
        },
    )


@app.get("/upload-sw.js", tags=["frontend"], include_in_schema=False)
async def upload_service_worker() -> FileResponse:
    """Upload SW — `importScripts()`-able from /sw.js. Lives at root for
    the same scope reason."""
    return FileResponse(
        os.path.join(_FRONTEND_STATIC, "js", "upload-sw.js"),
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache, must-revalidate",
        },
    )


# StaticFiles must mount AFTER the explicit /sw.js + /upload-sw.js routes
# above — they need first dibs on those paths. Everything under
# `frontend/static/` is otherwise served at /static/...
if os.path.isdir(_FRONTEND_STATIC):
    app.mount(
        "/static",
        StaticFiles(directory=_FRONTEND_STATIC, follow_symlink=False),
        name="static",
    )
else:
    logger.warning(
        "Frontend static dir not found at %s — page templates will fail.",
        _FRONTEND_STATIC,
    )

# Page routes register LAST so the static + service-worker handlers win
# at path collision time (e.g. if a template route ever shadowed /sw.js).
app.include_router(pages_router.router)
