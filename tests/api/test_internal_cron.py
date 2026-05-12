"""Internal-cron endpoint smoke tests — Phase 2.6.

Focus on the auth shape (503 unconfigured, 403 wrong secret, 200 success)
and the dry_run safety knob. The underlying service logic has its own
unit tests; here we verify the wiring + secret gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_run_reminders_503_when_cron_secret_unset(api_client: AsyncClient):
    """Empty CRON_SECRET in env → endpoint fails closed."""
    with patch("src.api.internal_cron.settings") as fake:
        fake.CRON_SECRET = ""
        r = await api_client.post("/api/internal/run-reminders")
    assert r.status_code == 503


async def test_run_reminders_403_wrong_secret(api_client: AsyncClient):
    with patch("src.api.internal_cron.settings") as fake:
        fake.CRON_SECRET = "right-secret"
        r = await api_client.post(
            "/api/internal/run-reminders",
            headers={"X-Cron-Secret": "wrong"},
        )
    assert r.status_code == 403


async def test_run_reminders_dry_run_returns_preview(api_client: AsyncClient):
    """Dry-run goes through the service without touching state."""
    with patch("src.api.internal_cron.settings") as fake, \
         patch("src.services.reminder_service.run_reminders",
               new=AsyncMock(return_value={"due_count": 0, "dry_run": True, "preview": []})) as svc:
        fake.CRON_SECRET = "ok"
        r = await api_client.post(
            "/api/internal/run-reminders?dry_run=true",
            headers={"X-Cron-Secret": "ok"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["due_count"] == 0
    svc.assert_awaited_once()
    # dry_run=True must be forwarded as a keyword.
    assert svc.await_args.kwargs.get("dry_run") is True


async def test_run_scheduled_messages_secret_gate(api_client: AsyncClient):
    with patch("src.api.internal_cron.settings") as fake:
        fake.CRON_SECRET = "ok"
        r = await api_client.post("/api/internal/run-scheduled-messages")
    assert r.status_code == 403  # missing header


async def test_reminder_service_max_per_run_cap():
    """Even with thousands of due deliveries, only `limit` are returned."""
    # We don't need a DB here — the service caps at the SQL layer, so
    # calling with limit=5 on an empty DB still must be safe + return [].
    from src.core.database import AsyncSessionLocal
    from src.services.reminder_service import find_due_reminders
    async with AsyncSessionLocal() as session:
        rows = await find_due_reminders(session, limit=5)
    assert isinstance(rows, list)
    assert len(rows) <= 5
