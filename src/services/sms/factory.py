"""SMS provider factory — Phase 2.3 + 2.7a placeholders.

`get_sms_provider()` returns the active SMS provider based on the
`SMS_PROVIDER` env var. The mock is the only fully-implemented provider
today; the other names (twilio / inforu / meta_whatsapp / o19) are
PLACEHOLDERS — they raise NotImplementedError with instructions until a
real adapter lands.

Why placeholders instead of just raising "unknown provider"? So the
moment someone flips `SMS_PROVIDER=twilio` in env, they get a clear,
specific error pointing them at the file they need to create — not a
generic "unknown" that looks like a typo.

How to add a real provider (when the time comes):

1. Create `src/services/sms/twilio.py` (or inforu.py, etc.).
2. `class TwilioSMSProvider(RealSMSProvider):` — inherit from
   `RealSMSProvider` in `base.py` so kill-switch + whitelist + audit
   apply automatically.
3. Set `name = "twilio"`.
4. Implement `async def _send_via_provider(self, phone, message)
   -> SMSResult`. This is the ONLY method to write — it just does the
   HTTP call and returns the SMSResult.
5. Replace the placeholder branch below with a real import + return.
6. Provider-specific env vars (`SMS_TWILIO_ACCOUNT_SID`,
   `SMS_TWILIO_AUTH_TOKEN`, `SMS_TWILIO_FROM`) are already declared in
   `src/core/config.py` — fill them in `.env`.
"""

from __future__ import annotations

from src.core.config import settings
from src.services.sms.base import SMSProvider
from src.services.sms.mock import MockSMSProvider

_PROVIDER_NOT_IMPLEMENTED_MSG = (
    "SMS_PROVIDER={name!r} is reserved but not implemented yet. "
    "To enable: create src/services/sms/{module}.py that subclasses "
    "RealSMSProvider, fill SMS_{upper}_* env vars, and replace the "
    "placeholder branch in src/services/sms/factory.py."
)


def _not_implemented(name: str, module: str) -> SMSProvider:
    raise NotImplementedError(
        _PROVIDER_NOT_IMPLEMENTED_MSG.format(
            name=name, module=module, upper=module.upper().replace("_", ""),
        )
    )


def get_sms_provider() -> SMSProvider:
    """Return the configured SMS provider. Defaults to mock in dev/tests.

    Real providers go through `RealSMSProvider` in `base.py`, which
    automatically applies the kill switch + whitelist + audit safety
    rails defined in `safety.py`.
    """
    name = (settings.SMS_PROVIDER or "mock").lower().strip()
    if name in ("", "mock", "none", "console"):
        return MockSMSProvider()

    # Placeholder branches — each raises NotImplementedError until the
    # corresponding adapter module is written. The error message points
    # at the exact file to create.
    if name == "twilio":
        return _not_implemented("twilio", "twilio")
    if name == "inforu":
        return _not_implemented("inforu", "inforu")
    if name == "meta_whatsapp":
        return _not_implemented("meta_whatsapp", "meta_whatsapp")
    if name == "o19":
        return _not_implemented("o19", "o19")

    raise ValueError(f"Unknown SMS_PROVIDER {name!r}")


__all__ = ["get_sms_provider"]
