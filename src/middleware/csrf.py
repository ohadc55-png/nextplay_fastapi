"""CSRF protection middleware.

Mirror of `backend/auth/middleware.py:init_csrf_protection`. Defends against
form-style cross-origin POSTs by requiring one of:
- `X-Requested-With` header (any value), OR
- `Content-Type: application/json`

A cross-origin <form> can't set either of those without CORS preflight
permission, so the presence of one is sufficient evidence the request
came from our SPA's fetch/XHR code rather than a malicious form.

Bypass conditions (state-changing requests that are exempt):
- Path is in `_CSRF_EXEMPT_PREFIXES` (auth endpoints called before JS
  loads the SPA, OAuth callbacks, the internal cron endpoint).
- Authorization: Bearer header is present (mobile/API client — bearer
  itself is anti-CSRF since the browser would never send it on a forged
  request).

Read-only methods (GET / HEAD / OPTIONS) are always allowed.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

# Paths exempt from CSRF check. Mirrors `backend/auth/middleware.py:9-20`
# plus the internal cron endpoint which authenticates with CRON_SECRET
# instead of a session cookie.
_CSRF_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/verify-email",
    "/api/auth/resend-verification",
    "/auth/google/callback",
    "/auth/facebook/callback",
    "/auth/apple/callback",
    "/auth/google",
    "/auth/facebook",
    "/auth/apple",
    "/api/internal/",  # cron jobs use shared-secret auth, not CSRF
)

_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


class CSRFMiddleware(BaseHTTPMiddleware):
    """Enforces an X-Requested-With or JSON content-type header on all
    state-changing `/api/*` requests, with the bypass list above."""

    async def dispatch(self, request: Request, call_next) -> Response:  # noqa: ANN001
        # Cheap bail-outs first.
        if request.method in _SAFE_METHODS:
            return await call_next(request)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        if any(request.url.path.startswith(p) for p in _CSRF_EXEMPT_PREFIXES):
            return await call_next(request)

        # Bearer-tokened requests are trusted (mobile/API clients).
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            return await call_next(request)

        # Require either X-Requested-With or application/json.
        has_xrw = request.headers.get("X-Requested-With") is not None
        content_type = request.headers.get("Content-Type", "") or ""
        is_json = "application/json" in content_type.lower()

        if not (has_xrw or is_json):
            return JSONResponse(
                status_code=403,
                content={"detail": "Missing CSRF header", "code": "csrf_missing"},
            )

        return await call_next(request)


__all__ = ["CSRFMiddleware"]
