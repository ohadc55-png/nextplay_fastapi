"""FastAPI app entry point.

Phase 0: empty app with health check, CORS, Sentry, and a lifespan hook that
verifies DB connectivity. Routers, middleware, and services are mounted in
later phases.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from src.api import auth as auth_router
from src.api import email_auth as email_auth_router
from src.api import oauth as oauth_router
from src.core.config import settings
from src.core.database import AsyncSessionLocal, engine
from src.core.exceptions import AppError
from src.middleware.csrf import CSRFMiddleware
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
    except Exception as exc:  # noqa: BLE001
        logger.error("Database connectivity check failed at startup: %s", exc)
        # Don't raise — let the app boot so /healthz can report degraded state.

    # ChromaDB / S3 / migrations init will be wired in Phases 1, 5, 6.

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
    except Exception:  # noqa: BLE001
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "connected" if db_ok else "disconnected",
        "version": app.version,
        "environment": settings.RAILWAY_ENVIRONMENT or "local",
    }


# ---------------------------------------------------------------------------
# Routers (Phase 3 — auth)
# ---------------------------------------------------------------------------

app.include_router(auth_router.router)
app.include_router(email_auth_router.router)
app.include_router(oauth_router.router)

# Domain routers (chat, coach, teams, ...) land in Phase 4.
