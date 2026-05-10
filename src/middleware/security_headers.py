"""Security headers middleware.

Mirror of `backend/auth/middleware.py:init_security_headers`. Sets the same
headers + CSP allowlist so the cookies/iframes/CDNs the v1 frontend
depends on continue to work.

CSP nonce: a per-request random token is exposed via `request.state.csp_nonce`
so Jinja2 templates can render `<script nonce="{{ csp_nonce }}">` for inline
scripts that explicitly need it. The CSP header itself uses
`'unsafe-inline'` (matches v1) so unmarked inline scripts still execute —
v1's templates have many such scripts (loader fade-out, OAuth callback
flow, Service Worker registration) and the migration plan §1 says
"frontend unchanged". A `'nonce-...'` would override `'unsafe-inline'`
in modern browsers, so we deliberately omit the nonce from script-src
to keep v1 compatibility. Templates that DO want a nonce can still read
it from `g.csp_nonce` for defense-in-depth on dynamically-injected
script blocks (none in current code).
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from src.core.config import settings


def _build_csp(_nonce: str) -> str:
    """Render the CSP header value. Allowlist matches v1.0-flask
    (`backend/auth/middleware.py:43-58`) — Google OAuth, Apple, Facebook,
    YouTube, Vimeo, Video.js CDN, fonts, S3 (eu-central-1), CloudFront.

    `nonce` is accepted for forward compatibility but deliberately NOT
    inserted into `script-src` — see module docstring."""
    return (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' "
        "https://accounts.google.com "
        "https://appleid.cdn-apple.com https://connect.facebook.net "
        "https://vjs.zencdn.net https://www.youtube.com https://s.ytimg.com "
        "https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
        "https://vjs.zencdn.net https://cdn.jsdelivr.net; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https:; "
        "frame-src https://accounts.google.com https://appleid.apple.com "
        "https://www.facebook.com "
        "https://www.youtube.com https://player.vimeo.com; "
        "connect-src 'self' "
        "https://*.s3.amazonaws.com https://*.s3.eu-central-1.amazonaws.com "
        "https://*.cloudfront.net; "
        "media-src 'self' blob: "
        "https://*.s3.amazonaws.com https://*.s3.eu-central-1.amazonaws.com "
        "https://*.cloudfront.net; "
        "worker-src 'self'"
    )


# FastAPI's auto-generated documentation pages emit inline `<script>` blocks
# without our nonce attribute (the HTML template is fixed inside FastAPI),
# so a nonce-based CSP would block them. These paths are dev-only debug
# surfaces — skip CSP enforcement on them entirely. The other security
# headers (X-Frame-Options, X-Content-Type-Options, etc.) still apply.
_CSP_EXEMPT_PATHS: tuple[str, ...] = (
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds standard security headers + a per-request CSP nonce."""

    def __init__(self, app: ASGIApp, *, hsts_in_production: bool = True) -> None:
        super().__init__(app)
        self.hsts_in_production = hsts_in_production

    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate the nonce BEFORE the response is built so templates /
        # downstream code can read it.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # Hide framework / version
        response.headers["Server"] = "NextPlay"

        if self.hsts_in_production and settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        # Skip CSP on FastAPI's docs surfaces — see _CSP_EXEMPT_PATHS comment.
        path = request.url.path
        if not any(path == p or path.startswith(p + "/") for p in _CSP_EXEMPT_PATHS):
            response.headers["Content-Security-Policy"] = _build_csp(nonce)
        return response


__all__ = ["SecurityHeadersMiddleware"]
