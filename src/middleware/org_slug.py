"""OrgSlugMiddleware — extract the per-org slug from the URL path.

Phase 13 sibling to `OrgContextMiddleware`. Where OrgContext reads the SESSION
to learn which org the user *thinks* they're in, this middleware reads the
URL PATH to learn which org the URL is *pointing at*. The pair lets the
page-handler layer compare the two and raise 404 on mismatch (the cloak rule).

This is a pure, side-effect-free read:
- Looks at the FIRST path segment only (`request.url.path.lstrip("/").split("/", 1)[0]`).
- If it matches a known non-tenant prefix (`org`, `admin`, `api`, `static`,
  service workers, coach-app routes, etc.), it stamps `None`.
- Otherwise — if it passes the slug regex — stamps it as the candidate slug.
- NEVER hits the database. Slug-to-org-id resolution is the dependency
  layer's job (where we already have a DB session via `Depends(get_db)`).
- NEVER raises. Returning a `None` slug for an invalid path lets the route
  matcher fall through to the legacy `/org/{path:path}` redirect or a 404.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.services.org_validators import SLUG_RE


class OrgSlugMiddleware(BaseHTTPMiddleware):
    """Stamp `request.state.path_slug` from the first URL path segment.

    Always sets the attribute (to `None` when the first segment is a known
    non-tenant prefix), so downstream code can safely
    `getattr(request.state, "path_slug", None)`.
    """

    # Top-level path segments that are NOT org slugs. Listing them explicitly
    # keeps the middleware fast (O(1) set membership) and self-documenting.
    # If you add a new top-level route, add its prefix here too.
    NON_TENANT_PREFIXES: frozenset[str] = frozenset({
        # FastAPI app-level prefixes
        "org", "admin", "api", "static", "auth", "oauth",
        "health", "healthz",
        # Service workers (must be served from root for scope reasons)
        "sw.js", "upload-sw.js",
        # Coach-app v1 surface — every top-level route the coach app uses
        "home", "chat", "plays", "play", "notebook", "scouting", "history",
        "upgrade", "team-setup", "register", "login", "logout",
        "verify-email", "reset-password", "join", "invite-accept",
        "welcome", "privacy", "terms", "contact-sales", "checkout",
        "coach-profile", "coach-settings", "data-upload", "public", "clip",
        # Browser well-known paths
        "favicon.ico", "robots.txt", "sitemap.xml",
    })

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path or "/"
        first = path.lstrip("/").split("/", 1)[0]

        path_slug: str | None = None
        if first and first not in self.NON_TENANT_PREFIXES and SLUG_RE.match(first):
            path_slug = first

        request.state.path_slug = path_slug
        return await call_next(request)


__all__ = ["OrgSlugMiddleware"]
