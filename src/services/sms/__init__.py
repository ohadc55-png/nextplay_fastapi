"""SMS provider package — Phase 2.3.

Adapter pattern: code calls `get_sms_provider().send(phone, body)`. The
factory returns a Mock (logs only) in dev/tests, and is ready to plug in
Inforu / 019 / Twilio in Sub-Phase 2.7 without touching call sites.
"""

from src.services.sms.base import SMSProvider, SMSResult
from src.services.sms.factory import get_sms_provider

__all__ = ["SMSProvider", "SMSResult", "get_sms_provider"]
