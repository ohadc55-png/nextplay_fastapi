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
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.auth import AuthToken
from src.repositories.email_repo import EmailLogRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token generation helpers
# ---------------------------------------------------------------------------

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def _issue_auth_token(
    session: AsyncSession,
    *,
    user_id: int | None,
    purpose: str,
    expires_in_hours: int = 24,
) -> tuple[str, int]:
    """Mint a new auth_tokens row. Returns ``(raw_token, auth_token_id)``.

    Storage: only the SHA256 hash is persisted. The raw token is never
    written to disk or logged at INFO level. ``user_id=None`` is allowed
    only for purposes where the invitee/recipient may not yet have a
    `users` row (e.g. ``org_invite``)."""
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = datetime.now(UTC) + timedelta(hours=expires_in_hours)
    row = AuthToken(
        user_id=user_id,
        token_hash=token_hash,
        purpose=purpose,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return raw, row.id


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
    token, _token_id = await _issue_auth_token(
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
    token, _token_id = await _issue_auth_token(
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


async def send_org_invite_email(
    session: AsyncSession,
    *,
    inviter_display_name: str,
    invitee_email: str,
    invitee_user_id: int | None,
    organization_name: str,
    role: str,
    background=None,
) -> tuple[str, int]:
    """Issue an `org_invite` token (7-day TTL), schedule the actual Resend
    dispatch via `src.services.email`, and return ``(raw_token, auth_token_id)``
    so the caller can persist an OrgInvite row referencing the token id.

    The HTML and text bodies are simple inline strings (Phase 0 — no
    Jinja template lookup yet to keep dependencies tight). Phase 1 can
    swap to a templated render if branding requirements grow."""
    raw, token_id = await _issue_auth_token(
        session,
        user_id=invitee_user_id,
        purpose="org_invite",
        expires_in_hours=168,  # 7 days
    )
    accept_url = _link("/org/invite-accept", raw)

    safe_org = (organization_name or "").replace("<", "&lt;").replace(">", "&gt;")
    safe_role = (role or "").replace("<", "&lt;").replace(">", "&gt;")
    safe_inviter = (inviter_display_name or "A NextPlay admin").replace("<", "&lt;").replace(">", "&gt;")

    subject = f"You're invited to {organization_name} on NextPlay"
    text_body = (
        f"{inviter_display_name} invited you to join {organization_name} on "
        f"NextPlay as {role}. Accept this invitation:\n\n"
        f"{accept_url}\n\nThis link expires in 7 days."
    )
    html_body = (
        f"<p><strong>{safe_inviter}</strong> invited you to join "
        f"<strong>{safe_org}</strong> on NextPlay as <em>{safe_role}</em>.</p>"
        f'<p><a href="{accept_url}">Accept this invitation</a></p>'
        f"<p style=\"color:#888;font-size:12px;\">This link expires in 7 days.</p>"
    )

    # Schedule the actual send via Resend / console (Phase 7 dispatcher).
    # Late-imported to avoid a top-level circular dep with src.services.email.
    from src.services.email import schedule_email

    schedule_email(
        background,
        user_id=invitee_user_id,
        to_email=invitee_email,
        subject=subject,
        html=html_body,
        text=text_body,
        template="org_invite",
        kind="transactional",
    )
    return raw, token_id


__all__ = [
    "send_org_invite_email",
    "send_password_reset_email",
    "send_verification_email",
    "send_welcome_email",
]
