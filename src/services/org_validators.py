"""Shared validators for org slug + subdomain (Phase 1.8).

Originally `_validate_slug` lived inline in `src/api/admin_orgs.py`. The
wizard's preflight + commit endpoints need the same rules, so we centralize
them here. `admin_orgs.py` keeps its symbol via a thin re-export so existing
callers don't break.

Phase 13 — slug also becomes the leading URL segment for every tenant page
(`/<slug>/dashboard`, …). To avoid collisions with non-tenant top-level
paths (`/admin`, `/api`, `/static`, `/login`, etc.), we maintain a reserved
list and reject any creation request that targets one.
"""

from __future__ import annotations

import re

from src.core.exceptions import ValidationError

# Slug: 1-50 chars, lowercase alphanumeric + hyphens, no leading/trailing hyphen.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")

# Subdomain: looser cap (DNS label = 63 chars). Same character class.
SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# Slugs that would collide with top-level routes once we switch to
# `/<slug>/*` URLs in Phase 13. Verified once at deploy time against the
# `organizations` table — any pre-existing row that uses one of these
# would need a manual rename before the slug-URL flag flips.
RESERVED_SLUGS: frozenset[str] = frozenset({
    # FastAPI top-level prefixes
    "org", "admin", "api", "static", "auth", "oauth",
    "health", "healthz", "favicon.ico", "robots.txt", "sitemap.xml",
    "sw.js", "upload-sw.js",
    # Generic session / pre-org paths (these stay at /org/* + root)
    "login", "logout", "signup", "register", "join", "invite-accept",
    "role-select", "switch-role",
    # Coach-app v1 surface (avoid collision with any /<slug>/ that would
    # shadow an existing coach route)
    "home", "chat", "play", "plays", "team-setup", "notebook",
    "scouting", "history", "upgrade", "checkout", "settings",
    "verify-email", "reset-password", "privacy", "terms",
    # Future-proofing — names we may want to reserve before they're built
    "billing", "support", "help", "docs", "www", "app", "mail",
    "welcome", "contact-sales", "coach-profile", "coach-settings",
    "data-upload", "public", "clip",
})


def validate_slug(slug: str) -> str:
    """Normalize + validate an org slug. Returns the cleaned value or raises
    `ValidationError(code="invalid_slug" | "reserved_slug")`."""
    s = (slug or "").strip().lower()
    if not SLUG_RE.match(s):
        raise ValidationError(
            "Slug must be 1-50 chars, lowercase alphanumeric + hyphens, "
            "no leading or trailing hyphen.",
            code="invalid_slug",
        )
    if s in RESERVED_SLUGS:
        raise ValidationError(
            "This slug is reserved (would collide with a platform route).",
            code="reserved_slug",
        )
    return s


def validate_subdomain(value: str | None) -> str | None:
    """Normalize + validate an optional subdomain. Empty/None → None.
    Otherwise returns the cleaned value or raises `ValidationError`."""
    if value is None:
        return None
    s = value.strip().lower()
    if not s:
        return None
    if not SUBDOMAIN_RE.match(s):
        raise ValidationError(
            "Subdomain must be 1-63 chars, lowercase alphanumeric + hyphens, "
            "no leading or trailing hyphen.",
            code="invalid_subdomain",
        )
    return s


__all__ = [
    "RESERVED_SLUGS",
    "SLUG_RE",
    "SUBDOMAIN_RE",
    "validate_slug",
    "validate_subdomain",
]
