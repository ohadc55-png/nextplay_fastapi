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

def _coach_name_from_email(email: str | None, display_name: str | None = None) -> str:
    """Best-effort: pull a friendly first name from display_name; fall back
    to the email's local-part (capitalized). Used to personalize templates
    when the caller only has email + user_id on hand."""
    if display_name:
        # Use just the first whitespace-delimited word so "John Smith" → "John".
        first = display_name.strip().split()[0] if display_name.strip() else ""
        if first:
            return first
    if email and "@" in email:
        local = email.split("@", 1)[0]
        # Strip dots/underscores for the display version: "ohad.cohen" → "Ohad Cohen"
        nicer = local.replace(".", " ").replace("_", " ").replace("-", " ").strip()
        return nicer.title() if nicer else "Coach"
    return "Coach"


async def send_verification_email(
    session: AsyncSession,
    *,
    user_id: int,
    email: str,
    background=None,
    display_name: str | None = None,
    language: str = "en",
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

    from src.services.email_templates import render
    subject, html_body, text_body = render(
        "verify_email",
        language=language,
        context={
            "coach_name": _coach_name_from_email(email, display_name),
            "verify_url": link,
            "cta_url": link,
            "cta_label_en": "Verify email",
            "cta_label_he": "אמת את המייל",
        },
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
    display_name: str | None = None,
    language: str = "en",
) -> None:
    """Mint a 2-hour `reset_password` token, schedule the email dispatch."""
    token, _token_id = await _issue_auth_token(
        session, user_id=user_id, purpose="reset_password", expires_in_hours=2,
    )
    link = _link("/reset-password", token)

    from src.services.email_templates import render
    subject, html_body, text_body = render(
        "password_reset",
        language=language,
        context={
            "coach_name": _coach_name_from_email(email, display_name),
            "reset_url": link,
            "cta_url": link,
            "cta_label_en": "Reset password",
            "cta_label_he": "אפס סיסמה",
        },
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
    display_name: str | None = None,
    language: str = "en",
) -> None:
    """Lightweight welcome dispatched to new OAuth users (no token)."""
    base = (
        settings.APP_BASE_URL.rstrip("/")
        or settings.BASE_URL.rstrip("/")
        or "https://trynextplay.app"
    )
    chat_url = f"{base}/chat?agent=gm"

    from src.services.email_templates import render
    subject, html_body, text_body = render(
        "welcome",
        language=language,
        context={
            "coach_name": _coach_name_from_email(email, display_name),
            "cta_url": chat_url,
            "cta_label_en": "Meet your AI staff",
            "cta_label_he": "פגוש את הצוות שלך",
        },
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
    language: str = "en",
) -> tuple[str, int]:
    """Issue an `org_invite` token (7-day TTL), schedule the actual Resend
    dispatch via `src.services.email`, and return ``(raw_token, auth_token_id)``
    so the caller can persist an OrgInvite row referencing the token id.
    """
    raw, token_id = await _issue_auth_token(
        session,
        user_id=invitee_user_id,
        purpose="org_invite",
        expires_in_hours=168,  # 7 days
    )
    accept_url = _link("/org/invite-accept", raw)

    from src.services.email_templates import render
    subject, html_body, text_body = render(
        "org_invite",
        language=language,
        context={
            "inviter_name": inviter_display_name or "A NextPlay admin",
            "organization_name": organization_name,
            "role": role,
            "cta_url": accept_url,
            "cta_label_en": "Accept invitation",
            "cta_label_he": "קבל הזמנה",
        },
    )

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
