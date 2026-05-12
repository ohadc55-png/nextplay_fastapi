"""Anti-bot challenge for the signing OTP verify endpoint — Phase 2 closeout.

After 2 failed OTP verify attempts on the same delivery, the next attempt
must also solve a small challenge (CAPTCHA). This stops an attacker
from spamming the OTP brute-force in the short window between rate-limit
ticks.

Today's implementation is a simple arithmetic question ("כמה זה 7 + 3?")
because:
  - Zero third-party dependencies (Turnstile/hCaptcha need a domain key)
  - Works offline / in CI / for the audit-replay tests
  - Trivially accessible (no audio CAPTCHA gymnastics for elderly parents)
  - Stateless: the challenge is HMAC-signed; no DB row for the challenge
    itself

For production, swap `_issue_arithmetic_challenge` for a Turnstile-style
adapter that posts to the provider and returns a token verified later.
The endpoint contract (issue + verify) stays the same — only the
strategy class changes.

API contract:
  - Server issues `{question, expires_at, token}`.
  - Client returns `{answer, expires_at, token}` along with the OTP code.
  - Server verifies HMAC(secret, f"{answer}|{expires_at}") == token.

The HMAC secret is `settings.SESSION_SECRET_KEY` (already required for
sessions, so no new env var needed).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)


CHALLENGE_TTL_SECONDS = 600  # 10 minutes — enough time for a slow human
ATTEMPTS_BEFORE_CHALLENGE = 2  # 3rd attempt onwards demands a challenge


def _secret() -> bytes:
    """The HMAC key. Reuse the existing session secret rather than introducing
    a new env var — both are bound to the same trust boundary anyway."""
    return (settings.SESSION_SECRET_KEY or "dev-secret-do-not-use-in-prod").encode("utf-8")


def _sign(answer: int, expires_at: int) -> str:
    return hmac.new(
        _secret(),
        f"{answer}|{expires_at}".encode(),
        hashlib.sha256,
    ).hexdigest()


def issue_arithmetic_challenge() -> dict[str, Any]:
    """Return a fresh challenge dict. The caller embeds this in its 4xx
    response so the client can render and re-submit."""
    a = secrets.randbelow(8) + 2  # 2..9
    b = secrets.randbelow(8) + 2  # 2..9
    answer = a + b
    expires_at = int(time.time()) + CHALLENGE_TTL_SECONDS
    return {
        "question": f"כמה זה {a} + {b}?",
        "a": a,
        "b": b,
        "expires_at": expires_at,
        "token": _sign(answer, expires_at),
    }


def verify_challenge(
    *, answer: int | str | None, expires_at: int | str | None, token: str | None,
) -> bool:
    """Validate an HMAC-signed answer. Constant-time comparison."""
    if answer is None or expires_at is None or not token:
        return False
    try:
        answer_int = int(answer)
        expires_at_int = int(expires_at)
    except (TypeError, ValueError):
        return False
    if time.time() > expires_at_int:
        return False
    expected = _sign(answer_int, expires_at_int)
    return hmac.compare_digest(token, expected)


__all__ = [
    "ATTEMPTS_BEFORE_CHALLENGE",
    "CHALLENGE_TTL_SECONDS",
    "issue_arithmetic_challenge",
    "verify_challenge",
]
