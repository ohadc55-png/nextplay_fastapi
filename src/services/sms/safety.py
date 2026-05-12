"""SMS safety rails — Phase 2.7a.

Built before any real provider lands. Every future real provider
(Twilio / Inforu / Meta WhatsApp / ...) MUST inherit from
`RealSMSProvider` in `base.py`, which routes its outgoing send through
these checks. The kill switch and whitelist are env-driven, so flipping
them does not require a deploy of a Python change — just a config edit
plus restart.

Three layers, evaluated in this order before any HTTP call to a provider:

1. **Kill switch.** `SMS_KILL_SWITCH=true` makes every real provider return
   `success=False, error='kill_switch_active'`. Reason: a single env var
   should be enough to stop the entire fleet of providers from sending
   anything at all in case of a billing surprise, a leak, or a runaway
   reminder cron.

2. **Whitelist.** `SMS_ALLOWED_RECIPIENTS` is a comma-separated list of
   phone numbers. When non-empty, only numbers on the list actually hit
   the provider; everything else returns `success=False,
   error='not_in_whitelist'`. The mock provider ignores this — it's a
   guardrail for real providers only, used to do a controlled live test
   with the dev's own phone before flipping to org-wide.

3. **Audit log.** Every send attempt by a real provider — success,
   blocked-by-switch, blocked-by-whitelist, provider-failure — is
   recorded in `org_audit_logs` via `log_org_action`. Phone numbers are
   stored masked (last 4 digits only) to keep the audit table from
   becoming a leakable PII surface.

Mock provider is intentionally exempt from all three: it doesn't send
anything anyway, and we don't want dev/test runs to fail because someone
forgot to whitelist the fake number "050-0000000".
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.services.sms.base import SMSResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phone normalization (for whitelist comparison)
# ---------------------------------------------------------------------------


def _strip_phone(raw: str | None) -> str:
    """Reduce a phone string to digits only so '050-123-4567', '0501234567',
    and '+972501234567' all hash to comparable forms."""
    if not raw:
        return ""
    return "".join(ch for ch in raw if ch.isdigit())


def _mask_phone(raw: str | None) -> str:
    """Mask everything except the last 4 digits. Used in audit + log output
    so phone numbers don't leak in dashboards / Sentry breadcrumbs."""
    digits = _strip_phone(raw)
    if not digits:
        return "***"
    if len(digits) <= 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def kill_switch_active() -> bool:
    """Re-reads `settings.SMS_KILL_SWITCH` on every call so a config change
    takes effect on the next attempted send without requiring a restart."""
    return bool(getattr(settings, "SMS_KILL_SWITCH", False))


# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------


def _parse_whitelist() -> set[str]:
    raw = (getattr(settings, "SMS_ALLOWED_RECIPIENTS", "") or "").strip()
    if not raw:
        return set()
    return {_strip_phone(p) for p in raw.split(",") if _strip_phone(p)}


def whitelist_blocks(phone: str) -> bool:
    """Returns True when the whitelist is configured AND `phone` is not on it.

    An empty whitelist is treated as "block everything" for real providers
    — the only way a real provider sends is via an explicit list of
    permitted numbers. This is the inverse of the usual "empty == allow"
    behaviour; the asymmetry is deliberate after the local Resend incident
    that motivated this scaffolding.
    """
    allowed = _parse_whitelist()
    if not allowed:
        # Empty list → in real-provider mode, block everything.
        return True
    return _strip_phone(phone) not in allowed


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


# Outcome codes — written into audit_data.extra for each send attempt.
OUTCOME_SENT = "sent"
OUTCOME_BLOCKED_KILL_SWITCH = "blocked_kill_switch"
OUTCOME_BLOCKED_WHITELIST = "blocked_whitelist"
OUTCOME_PROVIDER_FAILURE = "provider_failure"


async def write_audit(
    session: AsyncSession | None,
    *,
    provider: str,
    phone: str,
    outcome: str,
    organization_id: int | None = None,
    message_id: int | None = None,
    delivery_id: int | None = None,
    error: str | None = None,
) -> None:
    """Append a single row to org_audit_logs. Best-effort — failures to
    write the audit row are logged and swallowed (we never want the
    audit-write itself to fail a send)."""
    extra: dict[str, Any] = {
        "provider": provider,
        "phone_masked": _mask_phone(phone),
        "outcome": outcome,
    }
    if message_id is not None:
        extra["message_id"] = message_id
    if delivery_id is not None:
        extra["delivery_id"] = delivery_id
    if error:
        extra["error"] = str(error)[:200]

    # Use the standard logger always — even if we can't persist the audit
    # row (session is None / DB unreachable), Sentry + uvicorn console
    # still capture every send attempt.
    logger.info(
        "[SMS-AUDIT] provider=%s outcome=%s phone=%s extra=%s",
        provider, outcome, _mask_phone(phone), extra,
    )

    if session is None or organization_id is None:
        return
    try:
        # Imported here to avoid a circular dep (org_audit_service imports
        # repositories which transitively import config which imports this).
        from src.services.org_audit_service import log_org_action

        await log_org_action(
            session,
            organization_id=organization_id,
            actor_user_id=None,
            actor_email=None,
            action="sms.provider.attempt",
            target_type="sms_send",
            target_id=delivery_id or message_id,
            request=None,
            extra=extra,
        )
    except Exception as e:  # pragma: no cover — never block on audit
        logger.warning("[SMS-AUDIT] failed to persist audit row: %s", e)


# ---------------------------------------------------------------------------
# Decision helper
# ---------------------------------------------------------------------------


def safety_decision(phone: str) -> tuple[bool, str | None]:
    """Single entry point a RealSMSProvider can call before dialling out.

    Returns (allowed, reason). `allowed=False` means "do NOT call the
    provider; return a failure result with `error=reason`".
    """
    if kill_switch_active():
        return False, OUTCOME_BLOCKED_KILL_SWITCH
    if whitelist_blocks(phone):
        return False, OUTCOME_BLOCKED_WHITELIST
    return True, None


def blocked_result(reason: str) -> SMSResult:
    """Shorthand for a uniform 'blocked' return shape."""
    return SMSResult(success=False, message_id=None, error=reason)


__all__ = [
    "OUTCOME_BLOCKED_KILL_SWITCH",
    "OUTCOME_BLOCKED_WHITELIST",
    "OUTCOME_PROVIDER_FAILURE",
    "OUTCOME_SENT",
    "blocked_result",
    "kill_switch_active",
    "safety_decision",
    "whitelist_blocks",
    "write_audit",
]
