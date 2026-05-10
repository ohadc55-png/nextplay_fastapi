"""Email service tests — console mode, Resend mode, retry, log writes.

Resend SDK is patched at the import site inside `_resend_send_sync` so
no real HTTP calls happen. Console mode logs through the same DB path
as Resend mode — that's the invariant we test."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from src.models.email import EmailLog
from src.services import email as email_module

# ---------------------------------------------------------------------------
# Console mode
# ---------------------------------------------------------------------------


class TestConsoleMode:
    async def test_console_mode_logs_as_sent(self, db_session, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "console")

        result = await email_module.send_email_now(
            db_session,
            user_id=1, to_email="coach@example.com",
            subject="Welcome", html="<h1>Hi</h1>", text="Hi",
            template="welcome",
        )
        assert result["status"] == "sent"
        assert result["provider_id"] == "console"
        # Log row is in the DB
        rows = list((await db_session.execute(select(EmailLog))).scalars().all())
        assert len(rows) == 1
        assert rows[0].status == "sent"
        assert rows[0].template == "welcome"
        assert rows[0].sent_at is not None

    async def test_console_mode_runs_without_resend_api_key(
        self, db_session, monkeypatch
    ):
        """The whole point of console mode — works on a laptop with no API key."""
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "console")
        monkeypatch.setattr(settings, "RESEND_API_KEY", "")
        result = await email_module.send_email_now(
            db_session, user_id=None, to_email="x@y.com",
            subject="s", html="h",
        )
        assert result["status"] == "sent"

    async def test_invalid_recipient_logged_as_failed(
        self, db_session, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "console")
        result = await email_module.send_email_now(
            db_session, user_id=1, to_email="not-an-email",
            subject="s", html="h",
        )
        assert result["status"] == "failed"
        assert "invalid" in result["error"]
        rows = list((await db_session.execute(select(EmailLog))).scalars().all())
        assert rows[0].status == "failed"

    async def test_subject_truncation(self, db_session, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "console")
        await email_module.send_email_now(
            db_session, user_id=1, to_email="x@y.com",
            subject="x" * 1000, html="h",
        )
        row = (await db_session.execute(select(EmailLog))).scalar_one()
        # email_log.subject capped at 255
        assert len(row.subject) == 255


# ---------------------------------------------------------------------------
# Resend mode (real send path, with SDK mocked)
# ---------------------------------------------------------------------------


class TestResendMode:
    async def test_happy_path_logs_provider_id(self, db_session, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "resend")
        monkeypatch.setattr(settings, "RESEND_API_KEY", "test-key")

        # Patch the sync helper so the SDK isn't actually called.
        with patch.object(
            email_module, "_resend_send_sync",
            return_value=(True, "msg-abc-123", None),
        ):
            result = await email_module.send_email_now(
                db_session, user_id=1, to_email="coach@x.com",
                subject="Test", html="<h1>Hi</h1>", template="test",
            )
        assert result["status"] == "sent"
        assert result["provider_id"] == "msg-abc-123"
        row = (await db_session.execute(select(EmailLog))).scalar_one()
        assert row.provider_id == "msg-abc-123"

    async def test_resend_failure_logs_error(self, db_session, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "resend")
        monkeypatch.setattr(settings, "RESEND_API_KEY", "test-key")

        with patch.object(
            email_module, "_resend_send_sync",
            return_value=(False, None, "RuntimeError: 403 forbidden"),
        ):
            result = await email_module.send_email_now(
                db_session, user_id=1, to_email="x@y.com",
                subject="Test", html="h", template="test",
            )
        assert result["status"] == "failed"
        assert "403" in result["error"]
        row = (await db_session.execute(select(EmailLog))).scalar_one()
        assert row.status == "failed"
        assert "403" in row.error

    async def test_missing_api_key_in_resend_mode_returns_error(
        self, db_session, monkeypatch
    ):
        """Even in resend mode, no API key → graceful failure (don't crash)."""
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "resend")
        monkeypatch.setattr(settings, "RESEND_API_KEY", "")

        # _resend_send_sync uses the live setting — no mock needed
        result = await email_module.send_email_now(
            db_session, user_id=1, to_email="x@y.com",
            subject="s", html="h",
        )
        assert result["status"] == "failed"
        assert "RESEND_API_KEY" in result["error"]


# ---------------------------------------------------------------------------
# From-name selection
# ---------------------------------------------------------------------------


class TestFromName:
    async def test_transactional_uses_tx_name(self, db_session, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "resend")
        monkeypatch.setattr(settings, "RESEND_API_KEY", "test")
        monkeypatch.setattr(settings, "EMAIL_FROM_NAME_TX", "NextPlay Team")
        monkeypatch.setattr(settings, "EMAIL_FROM_NAME_MK", "Ohad from NextPlay")
        monkeypatch.setattr(settings, "EMAIL_FROM", "team@nextplay.example")

        captured: dict = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return (True, "id-1", None)

        with patch.object(email_module, "_resend_send_sync", side_effect=_capture):
            await email_module.send_email_now(
                db_session, user_id=1, to_email="x@y.com",
                subject="Verify", html="h", kind="transactional",
            )
        assert captured["from_addr"] == "NextPlay Team <team@nextplay.example>"

    async def test_marketing_uses_mk_name(self, db_session, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "resend")
        monkeypatch.setattr(settings, "RESEND_API_KEY", "test")
        monkeypatch.setattr(settings, "EMAIL_FROM_NAME_MK", "Ohad from NextPlay")
        monkeypatch.setattr(settings, "EMAIL_FROM", "team@nextplay.example")

        captured: dict = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return (True, "id-2", None)

        with patch.object(email_module, "_resend_send_sync", side_effect=_capture):
            await email_module.send_email_now(
                db_session, user_id=1, to_email="x@y.com",
                subject="Trial ending", html="h", kind="marketing",
            )
        assert "Ohad from NextPlay" in captured["from_addr"]


# ---------------------------------------------------------------------------
# Background task — opens its own session, swallows errors
# ---------------------------------------------------------------------------


class TestBackgroundTask:
    async def test_task_opens_own_session(self, monkeypatch):
        """`send_email_task` ignores the request session (it's already closed)
        and uses AsyncSessionLocal. We verify it doesn't crash when no
        request session is in context."""
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "console")

        # The task should complete without raising even when called bare.
        await email_module.send_email_task(
            user_id=1, to_email="x@y.com",
            subject="Hi", html="<h1>Hi</h1>",
            template="welcome",
        )
        # No assertion needed — not raising IS the assertion.

    async def test_task_swallows_send_errors(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "resend")
        monkeypatch.setattr(settings, "RESEND_API_KEY", "test")

        with patch.object(
            email_module, "_resend_send_sync",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise — the response is already on the wire.
            await email_module.send_email_task(
                user_id=1, to_email="x@y.com",
                subject="s", html="h",
            )


# ---------------------------------------------------------------------------
# schedule_email — convenience wrapper
# ---------------------------------------------------------------------------


class TestSchedule:
    async def test_schedule_with_background_tasks(self, monkeypatch):
        """When a BackgroundTasks is provided, schedule_email registers
        a deferred task instead of running inline."""
        captured: list = []

        class _FakeBackground:
            def add_task(self, fn, **kw):
                captured.append((fn, kw))

        bg = _FakeBackground()
        email_module.schedule_email(
            bg,
            user_id=1, to_email="x@y.com",
            subject="s", html="h", template="t",
        )
        assert len(captured) == 1
        fn, kw = captured[0]
        assert fn is email_module.send_email_task
        assert kw["user_id"] == 1
        assert kw["to_email"] == "x@y.com"

    async def test_schedule_falls_back_to_create_task_when_no_bg(
        self, monkeypatch
    ):
        """If a caller forgets to pass background_tasks (rare but
        possible), schedule_email still runs the send via
        `asyncio.create_task` rather than dropping the email."""
        from src.core.config import settings
        monkeypatch.setattr(settings, "EMAIL_MODE", "console")

        # No assertion needed beyond "didn't raise".
        email_module.schedule_email(
            None,
            user_id=1, to_email="x@y.com",
            subject="s", html="h", template="t",
        )
        # Give the loop a tick so the orphan task can run; otherwise pytest
        # will warn about "task was destroyed but it is pending".
        import asyncio
        await asyncio.sleep(0.05)
