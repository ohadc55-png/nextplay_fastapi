"""Outbound email service — auth flow + org invite emails.

Each public function:
1. Mints a single-use AuthToken (where applicable — verify/reset/invite).
2. Builds a transactional HTML + text body (inline; no Jinja for now).
3. Schedules the actual dispatch via ``src.services.email.schedule_email``.

Dispatch behavior is governed by ``settings.EMAIL_MODE``:
- ``console`` — body is printed to stdout + email_log row with provider_id="console"
- ``resend``  — real Resend API call via the resend SDK

The dispatcher (`email.py`) writes the email_log row itself (with the
correct status='sent'/'failed') so callers here MUST NOT log it again,
or you'll get duplicate rows.

Callers may pass FastAPI ``BackgroundTasks`` to defer the send until the
HTTP response is out (preferred). When ``None`` is passed,
``schedule_email`` falls back to ``asyncio.create_task`` — also fire-and-
forget but slightly less ergonomic. Both paths NEVER raise.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.auth import AuthToken

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inline HTML helpers (Phase 0 — no Jinja for transactional emails yet)
# ---------------------------------------------------------------------------


def _esc(value: str | None) -> str:
    """Tiny HTML escape — only the chars that matter for our tag-free bodies."""
    if not value:
        return ""
    return (value
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _wrap_button(label: str, url: str) -> str:
    """Big inline-styled button. Mirrors a typical transactional-email CTA."""
    return (
        f'<p style="margin:24px 0;">'
        f'<a href="{_esc(url)}" '
        f'style="background:#0a0e14;color:#fff;text-decoration:none;'
        f'padding:12px 24px;border-radius:6px;display:inline-block;'
        f'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        f'font-size:14px;font-weight:600;">{_esc(label)}</a>'
        f'</p>'
    )


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
    background=None,
) -> None:
    """Mint a 72-hour `verify_email` token, schedule the email dispatch.

    The auth_tokens row is committed by the request's get_db dependency
    (immediate, in current session). The actual SMTP/Resend send is
    deferred — fires after the HTTP response goes out so registration
    doesn't block on email delivery.
    """
    token, _token_id = await _issue_auth_token(
        session, user_id=user_id, purpose="verify_email", expires_in_hours=72,
    )
    link = _link("/verify-email", token)

    subject = "Verify your NextPlay email"
    text_body = (
        "Welcome to NextPlay!\n\n"
        "Please verify your email address by opening this link:\n\n"
        f"{link}\n\n"
        "This link expires in 72 hours.\n\n"
        "If you didn't sign up for NextPlay, you can safely ignore this email."
    )
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:520px;color:#1a1a1a;">'
        "<h2 style=\"margin:0 0 16px;\">Welcome to NextPlay</h2>"
        "<p>Please verify your email address to finish setting up your account.</p>"
        f"{_wrap_button('Verify email', link)}"
        '<p style="font-size:13px;color:#666;">This link expires in 72 hours. '
        "If you didn't sign up for NextPlay, you can safely ignore this email.</p>"
        "</div>"
    )

    from src.services.email import schedule_email
    schedule_email(
        background,
        user_id=user_id,
        to_email=email,
        subject=subject,
        html=html_body,
        text=text_body,
        template="verify_email",
        kind="transactional",
    )


async def send_password_reset_email(
    session: AsyncSession,
    *,
    user_id: int,
    email: str,
    background=None,
) -> None:
    """Mint a 2-hour `reset_password` token, schedule the email dispatch."""
    token, _token_id = await _issue_auth_token(
        session, user_id=user_id, purpose="reset_password", expires_in_hours=2,
    )
    link = _link("/reset-password", token)

    subject = "Reset your NextPlay password"
    text_body = (
        "We received a request to reset the password for your NextPlay account.\n\n"
        "Open this link to choose a new password:\n\n"
        f"{link}\n\n"
        "This link expires in 2 hours.\n\n"
        "If you didn't request a password reset, you can ignore this email — "
        "your current password will remain unchanged."
    )
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:520px;color:#1a1a1a;">'
        "<h2 style=\"margin:0 0 16px;\">Reset your password</h2>"
        "<p>We received a request to reset the password for your NextPlay account.</p>"
        f"{_wrap_button('Reset password', link)}"
        '<p style="font-size:13px;color:#666;">This link expires in 2 hours. '
        "If you didn't request a password reset, you can ignore this email — "
        "your current password will remain unchanged.</p>"
        "</div>"
    )

    from src.services.email import schedule_email
    schedule_email(
        background,
        user_id=user_id,
        to_email=email,
        subject=subject,
        html=html_body,
        text=text_body,
        template="reset_password",
        kind="transactional",
    )


async def send_welcome_email(
    session: AsyncSession,  # noqa: ARG001 — kept for signature parity with v1
    *,
    user_id: int,
    email: str,
    background=None,
) -> None:
    """Lightweight welcome dispatched to new OAuth users (no token)."""
    base = settings.APP_BASE_URL.rstrip("/") or settings.BASE_URL.rstrip("/") or "https://nextplay.co"
    subject = "Welcome to NextPlay"
    text_body = (
        "Welcome aboard!\n\n"
        "Your NextPlay account is ready. You can sign in any time at:\n\n"
        f"{base}/login\n\n"
        "Need help getting started? Reply to this email — we're here."
    )
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:520px;color:#1a1a1a;">'
        "<h2 style=\"margin:0 0 16px;\">Welcome to NextPlay</h2>"
        "<p>Your account is ready. You can sign in any time at "
        f'<a href="{_esc(base)}/login">{_esc(base)}/login</a>.</p>'
        "<p>Need help getting started? Reply to this email — we're here.</p>"
        "</div>"
    )

    from src.services.email import schedule_email
    schedule_email(
        background,
        user_id=user_id,
        to_email=email,
        subject=subject,
        html=html_body,
        text=text_body,
        template="welcome",
        kind="transactional",
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
