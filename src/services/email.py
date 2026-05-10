"""Email service — async port of v1 `backend/email/sender.py`.

Phase 7 batch 1.

Modes (driven by `settings.EMAIL_MODE`):
  - "console"  → render to stdout + email_log row with provider_id="console"
                 (default for local dev — zero risk of spamming real users).
  - "resend"   → real Resend API call. Uses `asyncio.to_thread` because the
                 `resend` SDK is sync.

Failure policy: never raise. Email failures are logged; the caller's HTTP
response is unaffected — a chat-question doesn't fail because Resend is down.

Background-task discipline:
  - In-request callers pass `BackgroundTasks` and we schedule `send_email`
    so the response goes out first.
  - The task gets its own `AsyncSessionLocal` for the email_log INSERT —
    by the time it runs, `get_db` has already committed and closed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.models.email import EmailLog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _email_mode() -> str:
    return (settings.EMAIL_MODE or "console").strip().lower()


def _from_address() -> str:
    return (settings.EMAIL_FROM or "onboarding@resend.dev").strip()


def _from_name(kind: str = "transactional") -> str:
    """Two display names share one address — `transactional` for verify/reset/
    receipt (system-feel), `marketing` for trial reminders + broadcasts (personal-feel)."""
    if kind == "marketing":
        return (settings.EMAIL_FROM_NAME_MK or "Ohad from NextPlay").strip()
    return (settings.EMAIL_FROM_NAME_TX or "NextPlay Team").strip()


# ---------------------------------------------------------------------------
# email_log writer
# ---------------------------------------------------------------------------


async def _write_log(
    session: AsyncSession,
    *,
    user_id: int | None,
    to_email: str,
    template: str,
    subject: str,
    language: str,
    status: str,
    provider_id: str | None = None,
    error: str | None = None,
) -> None:
    """Best-effort INSERT into email_log. Swallows exceptions —
    a logging failure must never block the actual email."""
    try:
        sent_at = datetime.now(UTC) if status == "sent" else None
        session.add(EmailLog(
            user_id=user_id,
            to_email=(to_email or "")[:255],
            template=(template or "")[:60],
            subject=(subject or "")[:255],
            language=(language or "en")[:8],
            status=status,
            provider_id=provider_id,
            error=(error or None) and error[:500],
            sent_at=sent_at,
        ))
        await session.flush()
    except Exception as e:
        logger.debug("[email] log insert failed for %s: %s", to_email, e)


# ---------------------------------------------------------------------------
# Console mode (dev fallback)
# ---------------------------------------------------------------------------


def _safe_print(s: str) -> None:
    """Survives Windows cp1252 stdout (default Python on Windows)."""
    try:
        print(s)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(s.encode(enc, errors="replace").decode(enc, errors="replace"))


def _render_to_console(
    *,
    to_email: str,
    from_addr: str,
    subject: str,
    text: str,
    template: str,
) -> None:
    sep = "-" * 70
    _safe_print(f"\n{sep}\n[EMAIL/console]  template={template}")
    _safe_print(f"From:    {from_addr}")
    _safe_print(f"To:      {to_email}")
    _safe_print(f"Subject: {subject}")
    _safe_print(sep)
    _safe_print(text or "(no text body)")
    _safe_print(sep + "\n")


# ---------------------------------------------------------------------------
# Resend send (sync — wrapped in asyncio.to_thread)
# ---------------------------------------------------------------------------


def _resend_send_sync(
    *,
    from_addr: str,
    to_email: str,
    subject: str,
    html: str,
    text: str,
) -> tuple[bool, str | None, str | None]:
    """Single Resend API call. Returns (success, provider_id, error).
    Lazy-imports `resend` so dev environments without the package can still
    use console mode."""
    api_key = (settings.RESEND_API_KEY or "").strip()
    if not api_key:
        return False, None, "RESEND_API_KEY not set"
    try:
        import resend

        resend.api_key = api_key
        params: dict[str, Any] = {
            "from": from_addr,
            "to": [to_email],
            "subject": subject,
            "html": html,
        }
        if text:
            params["text"] = text
        resp = resend.Emails.send(params)
        provider_id = resp.get("id") if isinstance(resp, dict) else None
        return True, provider_id, None
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Public API — sync (rare; admin test-send) + async background-task
# ---------------------------------------------------------------------------


async def send_email_now(
    session: AsyncSession,
    *,
    user_id: int | None,
    to_email: str,
    subject: str,
    html: str,
    text: str = "",
    template: str = "ad-hoc",
    language: str = "en",
    kind: str = "transactional",
) -> dict:
    """Synchronous send through the provided session. Returns
    {status, provider_id, error}. Always logs to email_log. NEVER raises.

    Used by:
      - the async background-task wrapper below (most callers)
      - admin "test send" buttons that need the inline result
    """
    if not to_email or "@" not in to_email:
        await _write_log(
            session,
            user_id=user_id, to_email=to_email or "",
            template=template, subject=subject, language=language,
            status="failed", error="invalid recipient",
        )
        return {"status": "failed", "provider_id": None, "error": "invalid recipient"}

    from_addr = f"{_from_name(kind)} <{_from_address()}>"
    mode = _email_mode()

    if mode == "console":
        _render_to_console(
            to_email=to_email, from_addr=from_addr,
            subject=subject, text=text, template=template,
        )
        await _write_log(
            session,
            user_id=user_id, to_email=to_email,
            template=template, subject=subject, language=language,
            status="sent", provider_id="console",
        )
        return {"status": "sent", "provider_id": "console", "error": None}

    # Real Resend send — sync SDK off the event loop.
    ok, provider_id, error = await asyncio.to_thread(
        _resend_send_sync,
        from_addr=from_addr, to_email=to_email,
        subject=subject, html=html, text=text,
    )
    if ok:
        await _write_log(
            session,
            user_id=user_id, to_email=to_email,
            template=template, subject=subject, language=language,
            status="sent", provider_id=provider_id,
        )
        return {"status": "sent", "provider_id": provider_id, "error": None}

    logger.warning("[email] send failed for %s template=%s: %s", to_email, template, error)
    await _write_log(
        session,
        user_id=user_id, to_email=to_email,
        template=template, subject=subject, language=language,
        status="failed", error=error,
    )
    return {"status": "failed", "provider_id": None, "error": error}


async def send_email_task(
    *,
    user_id: int | None,
    to_email: str,
    subject: str,
    html: str,
    text: str = "",
    template: str = "ad-hoc",
    language: str = "en",
    kind: str = "transactional",
) -> None:
    """Background-task entrypoint. Opens its own session because by the
    time this runs, the request's `get_db` has already committed and
    closed. NEVER raises — pool exhaustion / DB outage / Resend hiccup
    all log + swallow."""
    try:
        async with AsyncSessionLocal() as session:
            try:
                await send_email_now(
                    session,
                    user_id=user_id,
                    to_email=to_email, subject=subject,
                    html=html, text=text, template=template,
                    language=language, kind=kind,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("[email] background task failed for %s", to_email)
    except Exception:
        logger.exception("[email] could not open session for background send")


def schedule_email(
    background,
    *,
    user_id: int | None,
    to_email: str,
    subject: str,
    html: str,
    text: str = "",
    template: str = "ad-hoc",
    language: str = "en",
    kind: str = "transactional",
) -> None:
    """Convenience wrapper for endpoint handlers.

    Use:
        schedule_email(background, user_id=user.id, to_email=...,
                       subject=..., html=..., template="verify_email")
    """
    if background is None:
        # If a caller forgot to pass background_tasks, fall back to
        # async-creating the task on the event loop. Worst case it runs
        # to completion before the response — better than dropping the email.

        # the dangling reference is acceptable because send_email_task
        # owns its own session + error handling.
        _task = asyncio.create_task(send_email_task(  # noqa: RUF006
            user_id=user_id, to_email=to_email,
            subject=subject, html=html, text=text,
            template=template, language=language, kind=kind,
        ))
        return
    background.add_task(
        send_email_task,
        user_id=user_id, to_email=to_email,
        subject=subject, html=html, text=text,
        template=template, language=language, kind=kind,
    )


__all__ = [
    "schedule_email",
    "send_email_now",
    "send_email_task",
]
