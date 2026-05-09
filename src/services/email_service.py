"""Outbound email service (stub for Phase 3 — Resend integration in Phase 7).

For now, every "send" logs the email content + writes a row to ``email_log``
so the audit trail is complete. The actual SMTP/Resend dispatch lands in
Phase 7. This is enough for Phase 3 because the auth flow only NEEDS the
audit trail + the auth_tokens table to be populated; the email link itself
is exercised only by manual testing during dev.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.auth import AuthToken
from src.repositories.email_repo import EmailLogRepository
from src.repositories.users_repo import UsersRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token generation helpers
# ---------------------------------------------------------------------------

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def _issue_auth_token(
    session: AsyncSession,
    *,
    user_id: int,
    purpose: str,
    expires_in_hours: int = 24,
) -> str:
    """Mint a new auth_tokens row, return the RAW token (caller emails it).

    Storage: only the SHA256 hash is persisted. The raw token is never
    written to disk or logged at INFO level."""
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
    row = AuthToken(
        user_id=user_id,
        token_hash=token_hash,
        purpose=purpose,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return raw


def _link(path: str, token: str) -> str:
    base = settings.APP_BASE_URL.rstrip("/") or settings.BASE_URL.rstrip("/")
    return f"{base}{path}?token={token}"


# ---------------------------------------------------------------------------
# Outbound emails — auth flows
# ---------------------------------------------------------------------------

async def send_verification_email(
    session: AsyncSession,
    *,
    user_id: int,
    email: str,
) -> None:
    """Issue a `verify_email` AuthToken + log the send. Phase 7 will add the
    Resend dispatch here."""
    token = await _issue_auth_token(
        session, user_id=user_id, purpose="verify_email", expires_in_hours=72,
    )
    link = _link("/verify-email", token)
    logger.info("[email] verification → %s | link=%s", email, link)
    await EmailLogRepository(session).create(
        _build_log_row(user_id=user_id, to_email=email, template="verify_email"),
    )


async def send_password_reset_email(
    session: AsyncSession,
    *,
    user_id: int,
    email: str,
) -> None:
    token = await _issue_auth_token(
        session, user_id=user_id, purpose="reset_password", expires_in_hours=2,
    )
    link = _link("/reset-password", token)
    logger.info("[email] reset-password → %s | link=%s", email, link)
    await EmailLogRepository(session).create(
        _build_log_row(user_id=user_id, to_email=email, template="reset_password"),
    )


async def send_welcome_email(
    session: AsyncSession,
    *,
    user_id: int,
    email: str,
) -> None:
    """Sent to new OAuth users. No token; just a hello."""
    logger.info("[email] welcome → %s", email)
    await EmailLogRepository(session).create(
        _build_log_row(user_id=user_id, to_email=email, template="welcome"),
    )


# ---------------------------------------------------------------------------
# email_log row builder
# ---------------------------------------------------------------------------

def _build_log_row(*, user_id: int | None, to_email: str, template: str):
    """Construct an EmailLog row for the audit trail. status is 'pending'
    until Phase 7's actual sender flips it."""
    from src.models.email import EmailLog

    return EmailLog(
        user_id=user_id,
        to_email=to_email,
        template=template,
        status="pending",  # Phase 7 will update to 'sent' / 'failed'
        sent_at=None,
    )


__all__ = [
    "send_verification_email",
    "send_password_reset_email",
    "send_welcome_email",
]
