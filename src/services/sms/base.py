"""SMS provider base classes — Phase 2.3 + 2.7a safety wiring."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SMSResult:
    """Return value for SMSProvider.send.

    Providers MUST NOT raise on send failures — they encode the error in
    `error` and return `success=False` so callers can decide whether to
    fall back to email or mark a DocumentDelivery as FAILED.
    """

    success: bool
    message_id: str | None = None
    error: str | None = None


class SMSProvider(ABC):
    """Adapter base class. Implementations live in `mock.py`, plus future
    real providers (Sub-Phase 2.7 scaffolding lives in `safety.py` +
    `RealSMSProvider` below)."""

    # Identifier used in audit logs. Real providers should override.
    name: str = "abstract"

    @abstractmethod
    async def send(self, phone: str, message: str) -> SMSResult:
        """Send `message` to `phone`. Phone format normalization is the
        provider's job — they know their gateway's expectations."""
        raise NotImplementedError


class RealSMSProvider(SMSProvider):
    """Base for ANY provider that contacts a real gateway — Phase 2.7a.

    Subclasses implement `_send_via_provider(phone, message)` with the
    actual HTTP call. The public `send(...)` here applies the safety
    rails BEFORE the call: kill switch, whitelist, and audit logging.

    Why this lives in the base class: a future Twilio or Inforu adapter
    is a single `_send_via_provider` implementation away from being safe
    by default. There's no path that calls a provider without going
    through these checks.
    """

    name: str = "real-abstract"

    async def send(self, phone: str, message: str) -> SMSResult:
        # Local import to avoid a circular dep (safety imports config which
        # may import provider modules at startup time).
        from src.services.sms.safety import (
            OUTCOME_PROVIDER_FAILURE,
            OUTCOME_SENT,
            blocked_result,
            safety_decision,
            write_audit,
        )

        allowed, reason = safety_decision(phone)
        if not allowed:
            await write_audit(
                None, provider=self.name, phone=phone, outcome=reason or "blocked",
            )
            return blocked_result(reason or "blocked")

        try:
            result = await self._send_via_provider(phone, message)
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("[%s] provider raised: %s", self.name, e)
            await write_audit(
                None, provider=self.name, phone=phone,
                outcome=OUTCOME_PROVIDER_FAILURE, error=str(e),
            )
            return SMSResult(success=False, message_id=None, error=f"provider_exception:{e}")

        await write_audit(
            None, provider=self.name, phone=phone,
            outcome=OUTCOME_SENT if result.success else OUTCOME_PROVIDER_FAILURE,
            error=result.error,
        )
        return result

    @abstractmethod
    async def _send_via_provider(self, phone: str, message: str) -> SMSResult:
        """Make the real HTTP call. Subclasses MUST NOT raise on provider
        errors — wrap them in SMSResult(success=False, error=...)."""
        raise NotImplementedError


__all__ = ["RealSMSProvider", "SMSProvider", "SMSResult"]
