"""Frontend integration — Jinja2Templates + Flask compatibility shims.

Phase 8 batch 1. Templates copied verbatim from `v1.0-flask/frontend/`,
so they still call `url_for('static', filename='...')` and reference
`g.user`, `g.csp_nonce`, etc. We provide thin shims as Jinja2 globals
+ a per-request context builder so the templates work unchanged.

Why shim instead of search/replace? The 27 templates reference these
patterns ~hundreds of times. A shim is one piece of code; rewrites
would touch every template and create a divergence we'd have to
keep in sync with v1 until cutover.

Master prompt §1: "frontend/ unchanged. 26 JS files + 61 templates work
without modification."
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.requests import Request as StarletteRequest

from src.core.config import settings

# ---------------------------------------------------------------------------
# Flask compatibility shim: `request.args` → Starlette `query_params`
# ---------------------------------------------------------------------------
# v1 templates use Flask's `request.args.get('key')` syntax. Starlette
# exposes the same data as `request.query_params` with the same `.get()`
# method, so we monkey-patch an alias property at module import. Done
# once on the Request class — affects every template that touches
# `request.args.x`. Cheap and reversible.

if not hasattr(StarletteRequest, "args"):
    StarletteRequest.args = property(  # type: ignore[attr-defined]
        lambda self: self.query_params
    )

_TEMPLATES_DIR = "frontend/templates"
_STATIC_MOUNT = "/static"


# ---------------------------------------------------------------------------
# url_for shim
# ---------------------------------------------------------------------------


_ADMIN_ROUTES: dict[str, str] = {
    # Map Flask-style endpoint names (used verbatim in admin templates
    # ported from v1) to the FastAPI paths defined in src/api/admin_pages.py.
    # Keep in sync when admin routes are added or renamed.
    "admin.login":              "/admin/login",
    "admin.logout":             "/admin/logout",
    "admin.dashboard":          "/admin/dashboard",
    "admin.users":              "/admin/users",
    "admin.api_costs":          "/admin/api-costs",
    "admin.feature_usage":      "/admin/feature-usage",
    "admin.feedback":           "/admin/feedback",
    "admin.geography":          "/admin/geography",
    "admin.user_activity":      "/admin/user-activity",
    "admin.sales_inquiries":    "/admin/sales-inquiries",
    "admin.research_sources":   "/admin/research-sources",
    "admin.email_log_view":     "/admin/email-log",
    "admin.mailing_lists_view": "/admin/email-lists",
    "admin.email_compose_view": "/admin/email-compose",
    "admin.orgs":               "/admin/orgs",
    "admin.org_detail":         "/admin/orgs/{org_id}",
    "admin.orgs_wizard":        "/admin/orgs/wizard",
}


def _url_for(endpoint: str, **values: Any) -> str:
    """Flask-compatible `url_for` for the patterns the v1 templates use.

    Handles:
      - `url_for('static', filename='...')` → /static/...
      - `url_for('admin.<name>', **query)` → /admin/<path>?<query>
        (admin templates ported from v1's Flask blueprints — without
        this map their links rendered as `#` and clicking did nothing)
    Unknown endpoints return "#" so a typo doesn't crash a template render.
    """
    if endpoint == "static":
        filename = values.get("filename", "")
        return f"{_STATIC_MOUNT}/{filename.lstrip('/')}"

    base = _ADMIN_ROUTES.get(endpoint)
    if base is None:
        return "#"

    # Fill in any {placeholder} segments from kwargs; remaining kwargs
    # become the query string.
    path_kwargs: dict[str, Any] = {}
    query_kwargs: dict[str, Any] = {}
    for k, v in values.items():
        if v is None:
            continue
        if "{" + k + "}" in base:
            path_kwargs[k] = v
        else:
            query_kwargs[k] = v
    if path_kwargs:
        base = base.format(**path_kwargs)
    if query_kwargs:
        from urllib.parse import urlencode
        return f"{base}?{urlencode(query_kwargs)}"
    return base


# ---------------------------------------------------------------------------
# `g` and `config` shims
# ---------------------------------------------------------------------------


class _Box:
    """Dotted-attribute access wrapper for dicts. Templates do
    `g.user.display_name` and `g.user.email` — backed by the User model
    in production, by a dict here so anonymous pages still work."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, name: str) -> Any:
        # Missing attribute → empty string. Mirrors Flask's silent
        # behavior on `g.<unknown>` inside `{{ }}`.
        return ""


class _ConfigShim:
    """Stand-in for Flask's `config` in templates. The only key v1
    references is `CSS_VERSION` — bump it to bust browser caches when
    deploying new asset builds."""

    def __init__(self, css_version: str = "2") -> None:
        self._css_version = css_version

    def get(self, key: str, default: Any = None) -> Any:
        if key == "CSS_VERSION":
            return self._css_version
        return default


# ---------------------------------------------------------------------------
# Asset version helpers — used by base.html for `?v=...` cache busting
# ---------------------------------------------------------------------------


def _use_min() -> bool:
    """Use minified assets when running in production. Mirrors v1
    flask_app.py:328 — `bool(os.environ.get("RAILWAY_ENVIRONMENT"))`."""
    return settings.is_production


def _min_suffix() -> str:
    return ".min" if _use_min() else ""


# ---------------------------------------------------------------------------
# Templates singleton with globals registered
# ---------------------------------------------------------------------------


def _build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    env = templates.env
    env.globals["url_for"] = _url_for
    env.globals["config"] = _ConfigShim(css_version="27")
    # `g` is normally per-request; we expose a fallback empty Box so
    # templates rendered outside a request context (eg, error pages)
    # don't NameError. The page-route handlers override it via the
    # context builder below.
    env.globals["g"] = _Box(user=_Box(), csp_nonce="")
    return templates


templates = _build_templates()


# ---------------------------------------------------------------------------
# Per-request context builder
# ---------------------------------------------------------------------------


def page_context(
    request: Request,
    *,
    user: Any = None,
    org_ctx: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the context dict for a `templates.TemplateResponse(...)`
    call. Bundles every global v1's context processors injected:

      - `g` with `user` + `csp_nonce` (from the security_headers middleware)
      - `request` (FastAPI requirement for TemplateResponse)
      - `use_min`, `min_suffix` (asset minifier helpers)
      - `subscription_plan`, `trial_days_left`, `days_until_purge`,
        `is_club_member`, `is_club_admin`, `club` (subscription
        context — defaults to Nones for anon pages)

    Pass any page-specific data via `extra=...`.
    """
    nonce = getattr(request.state, "csp_nonce", "")
    g = _Box(user=user or _Box(), csp_nonce=nonce)

    plan = "trial"
    trial_days_left = 0
    trial_hours_left = 0
    days_until_purge = 0
    is_club_member = False
    is_club_admin = False
    club = None
    if user is not None and hasattr(user, "subscription_plan"):
        plan = (user.subscription_plan or "trial")
        # trial_days_left / days_until_purge are computed from
        # trial_ends_at / data_purge_at — done in Phase 4 services
        # but page rendering doesn't need exact values, just non-None
        # so the navbar's countdown widget renders.
        trial_days_left = max(
            0, _days_left(getattr(user, "trial_ends_at", None)),
        ) if plan == "trial" else 0
        # `trial_hours_left` powers the "Trial ends in 4h" banner when
        # `trial_days_left == 0`, so the user gets a real-time signal in
        # the final 24h rather than a stuck "0 days left" message.
        trial_hours_left = _hours_left(
            getattr(user, "trial_ends_at", None),
        ) if plan == "trial" else 0
        days_until_purge = max(
            0, _days_left(getattr(user, "data_purge_at", None)),
        ) if plan == "expired" else 0
        is_club_admin = bool(getattr(user, "is_club_admin", False))

    ctx: dict[str, Any] = {
        "request": request,
        "g": g,
        "use_min": _use_min(),
        "min_suffix": _min_suffix(),
        "subscription_plan": plan,
        "trial_days_left": trial_days_left,
        "trial_hours_left": trial_hours_left,
        "days_until_purge": days_until_purge,
        "is_club_member": is_club_member,
        "is_club_admin": is_club_admin,
        "club": club,
        # Org Admin (Phase 1) context — used by /org/* templates only. Coach App
        # pages don't pass org_ctx; the value stays None and the templates that
        # don't reference it stay unaffected.
        "org_ctx": org_ctx,
        "config": _ConfigShim(css_version="27"),
    }
    if extra:
        ctx.update(extra)
    return ctx


def _days_left(target) -> int:
    """Days from now until `target` (a datetime, ISO string, or None).
    Negative when past — clamped to 0 by the caller.

    `User.trial_ends_at` is stored as TEXT (ISO string) in the legacy
    schema; `User.data_purge_at` is a real DateTime. Handle both.
    """
    if target is None:
        return 0
    from datetime import datetime

    try:
        if isinstance(target, str):
            target = datetime.fromisoformat(target)
        now = datetime.now(UTC)
        if getattr(target, "tzinfo", None) is None:
            target = target.replace(tzinfo=UTC)
        delta = target - now
        return delta.days
    except Exception:
        return 0


def _hours_left(target) -> int:
    """Whole hours from now until `target`. Returns 0 if past/missing.

    Used for the "Trial ends in 4h" banner when `_days_left` already
    rounded down to 0 — same input semantics as `_days_left`.
    """
    if target is None:
        return 0
    from datetime import datetime

    try:
        if isinstance(target, str):
            target = datetime.fromisoformat(target)
        now = datetime.now(UTC)
        if getattr(target, "tzinfo", None) is None:
            target = target.replace(tzinfo=UTC)
        delta = target - now
        return max(0, int(delta.total_seconds() // 3600))
    except Exception:
        return 0


__all__ = [
    "_STATIC_MOUNT",
    "_TEMPLATES_DIR",
    "page_context",
    "templates",
]
