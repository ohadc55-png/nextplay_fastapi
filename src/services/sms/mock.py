"""Mock SMS provider — Phase 2.3.

Logs every "sent" message to the standard logger at INFO. That's all.
- No DB writes (matches the plan §2 "MockSMSLog skipped"). Visible in the
  uvicorn console + Sentry breadcrumbs.
- Process-local memory buffer (`SENT_LOG`) lets tests assert on what was
  "sent" without intercepting the logger handler.

Tests should NOT depend on the buffer being persistent across processes —
it's a dev tool, not state.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from dataclasses import dataclass

from src.services.sms.base import SMSProvider, SMSResult

logger = logging.getLogger(__name__)


@dataclass
class MockSentMessage:
    phone: str
    body: str
    message_id: str


# Bounded ring buffer — tests / admin diagnostics can read this without
# pulling external logs. 200 entries is plenty for a single test run.
SENT_LOG: deque[MockSentMessage] = deque(maxlen=200)


class MockSMSProvider(SMSProvider):
    async def send(self, phone: str, message: str) -> SMSResult:
        msg_id = f"mock-{uuid.uuid4().hex[:12]}"
        # Hebrew-friendly log line — visible in uvicorn console during dev.
        logger.info("[MOCK SMS] To: %s | Body: %s | id=%s", phone, message, msg_id)
        SENT_LOG.append(MockSentMessage(phone=phone, body=message, message_id=msg_id))
        return SMSResult(success=True, message_id=msg_id)


__all__ = ["MockSMSProvider", "MockSentMessage", "SENT_LOG"]
