"""SMS provider factory — Phase 2.3."""

from __future__ import annotations

from src.core.config import settings
from src.services.sms.base import SMSProvider
from src.services.sms.mock import MockSMSProvider


def get_sms_provider() -> SMSProvider:
    """Return the configured SMS provider. Defaults to mock in dev/tests.

    Future providers (Inforu / 019) will plug in here via additional
    `elif` branches. The plan keeps the signature stable so the call sites
    don't change in Sub-Phase 2.7.
    """
    name = (settings.SMS_PROVIDER or "mock").lower()
    if name in ("", "mock", "none", "console"):
        return MockSMSProvider()
    # When the real providers land, branch here:
    # if name == "inforu":
    #     from src.services.sms.inforu import InforuSMSProvider
    #     return InforuSMSProvider()
    raise ValueError(f"Unknown SMS_PROVIDER {name!r}")


__all__ = ["get_sms_provider"]
