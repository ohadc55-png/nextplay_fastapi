"""Shared validators for org slug + subdomain (Phase 1.8).

Originally `_validate_slug` lived inline in `src/api/admin_orgs.py`. The
wizard's preflight + commit endpoints need the same rules, so we centralize
them here. `admin_orgs.py` keeps its symbol via a thin re-export so existing
callers don't break.
"""

from __future__ import annotations

import re

from src.core.exceptions import ValidationError

# Slug: 1-50 chars, lowercase alphanumeric + hyphens, no leading/trailing hyphen.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")

# Subdomain: looser cap (DNS label = 63 chars). Same character class.
SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_slug(slug: str) -> str:
    """Normalize + validate an org slug. Returns the cleaned value or raises
    `ValidationError(code="invalid_slug")`."""
    s = (slug or "").strip().lower()
    if not SLUG_RE.match(s):
        raise ValidationError(
            "Slug must be 1-50 chars, lowercase alphanumeric + hyphens, "
            "no leading or trailing hyphen.",
            code="invalid_slug",
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


__all__ = ["SLUG_RE", "SUBDOMAIN_RE", "validate_slug", "validate_subdomain"]
