"""OrgContextMiddleware — resolves the active org from the session cookie.

Layer 1 of the three-layer multi-tenancy defense (middleware → repository →
Postgres RLS). Pure read; never raises. Stamps `request.state.org_id` and
`request.state.org_role` so downstream code (the `get_db` dependency for RLS,
and the `/org/*` route dependencies for membership lookup) can read them
without re-deriving from the session cookie.

Reads three Starlette session keys (set by `/org/login`):
- `ORG_SESSION_KEY` (`"org_user_id"`) — the logged-in Org Admin user id
- `ORG_ACTIVE_ORG_KEY` (`"org_active_org_id"`) — the currently selected org id
- `ORG_ACTIVE_ROLE_KEY` (`"org_active_role"`) — the currently selected role

This middleware does NOT validate that the user is still a member or that
the role is allowed — that's the dependency layer's job. It just exposes the
session intent on `request.state` so the dependency can re-validate cheaply.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Imported here (rather than from src.api.deps.org_auth) to avoid a circular
# import — the deps module imports from this middleware via request.state.
ORG_SESSION_KEY = "org_user_id"
ORG_ACTIVE_ORG_KEY = "org_active_org_id"
ORG_ACTIVE_ROLE_KEY = "org_active_role"


class OrgContextMiddleware(BaseHTTPMiddleware):
    """Stamp `request.state.org_id` / `org_role` from the session cookie.

    Always sets both attributes (to `None` if the session is empty) so that
    downstream `getattr(request.state, "org_id", None)` is always well-defined.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        org_id: int | None = None
        org_role: str | None = None
        org_user_id: int | None = None

        # Reading `request.session` requires SessionMiddleware to have run.
        # SessionMiddleware is registered AFTER this middleware in main.py
        # (Starlette wraps in reverse-add order), so the session is available.
        try:
            session = request.session
        except (AssertionError, AttributeError):
            session = None  # type: ignore[assignment]

        if session is not None:
            raw_user = session.get(ORG_SESSION_KEY)
            raw_org = session.get(ORG_ACTIVE_ORG_KEY)
            raw_role = session.get(ORG_ACTIVE_ROLE_KEY)
            if isinstance(raw_user, int):
                org_user_id = raw_user
            if isinstance(raw_org, int):
                org_id = raw_org
            if isinstance(raw_role, str):
                org_role = raw_role

        request.state.org_user_id = org_user_id
        request.state.org_id = org_id
        request.state.org_role = org_role

        return await call_next(request)


__all__ = [
    "ORG_ACTIVE_ORG_KEY",
    "ORG_ACTIVE_ROLE_KEY",
    "ORG_SESSION_KEY",
    "OrgContextMiddleware",
]
