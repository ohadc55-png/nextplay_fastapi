"""SMS provider base classes — Phase 2.3."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


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
    `inforu.py` / `o19.py` modules (Sub-Phase 2.7)."""

    @abstractmethod
    async def send(self, phone: str, message: str) -> SMSResult:
        """Send `message` to `phone`. Phone format normalization is the
        provider's job — they know their gateway's expectations."""
        raise NotImplementedError


__all__ = ["SMSProvider", "SMSResult"]
