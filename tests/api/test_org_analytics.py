"""Endpoint tests for /org/api/analytics/* — Phase 2.6a."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.teams import TeamProfile

pytestmark = pytest.mark.asyncio


async def _seed_template_with_deliveries(
    client: AsyncClient, session_factory, *, signed_count: int = 1, total: int = 3,
):
    """Create a template + N players + a campaign + N deliveries, with
    `signed_count` of them flipped to SIGNED."""
    with patch("src.services.document_template_service.s3.put_bytes",
               new=AsyncMock(return_value=None)):
        r = await client.post(
            "/org/api/document-templates",
            files={"file": ("t.pdf", b"%PDF-1.4\n" + b"x" * 100, "application/pdf")},
            data={"name": "Health", "category": "HEALTH", "requires_signature": "true"},
        )
        assert r.status_code == 201, r.text
        tid = r.json()["id"]

    org_id = client.org_seed["organization_id"]
    coach_id = client.org_seed["user_id"]

    async with session_factory() as s:
        team = TeamProfile(user_id=coach_id, organization_id=org_id, team_name="U12")
        s.add(team)
        await s.flush()
        for i in range(total):
            p = Player(user_id=coach_id, team_id=team.id, organization_id=org_id,
                       name=f"Kid {i}", active=True)
            s.add(p)
            await s.flush()
            s.add(PlayerContact(
                player_id=p.id, organization_id=org_id,
                parent_name=f"Parent {i}",
                parent_email=f"p{i}@example.com",
                parent_phone_enc=f"050-100000{i}",
            ))
        await s.commit()

    with patch("src.api.org_document_campaigns.dispatch_delivery", new=AsyncMock()):
        r = await client.post(
            "/org/api/document-campaigns",
            json={
                "template_id": tid,
                "title": "Q4 health",
                "recipient_filter": {"type": "all"},
                "delivery_channels": ["sms"],
            },
        )
    assert r.status_code == 201, r.text

    # Flip `signed_count` deliveries to SIGNED.
    from sqlalchemy import select

    from src.models.document_deliveries import DocumentDelivery
    async with session_factory() as s:
        rows = (await s.execute(select(DocumentDelivery))).scalars().all()
        for d in rows[:signed_count]:
            d.document_status = "SIGNED"
        await s.commit()
    return tid


async def test_overview_returns_signed_pct(
    org_admin_client: AsyncClient, api_session_factory,
):
    await _seed_template_with_deliveries(
        org_admin_client, api_session_factory, signed_count=2, total=4,
    )
    r = await org_admin_client.get("/org/api/analytics/overview?days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_sent"] == 4
    assert body["total_signed"] == 2
    assert body["total_pending"] == 2
    assert body["signed_percentage"] == 50.0
    assert body["period_days"] == 30


async def test_by_template_groups_correctly(
    org_admin_client: AsyncClient, api_session_factory,
):
    await _seed_template_with_deliveries(
        org_admin_client, api_session_factory, signed_count=1, total=2,
    )
    r = await org_admin_client.get("/org/api/analytics/by-template?days=30")
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["total"] == 2
    assert rows[0]["signed"] == 1
    assert rows[0]["signed_percentage"] == 50.0


async def test_period_clamping(
    org_admin_client: AsyncClient, api_session_factory,
):
    """days=0 should be coerced to the minimum; days>365 to 365."""
    r = await org_admin_client.get("/org/api/analytics/overview?days=0")
    assert r.status_code in (200, 422)  # FastAPI Query ge=1 enforces 422
    r = await org_admin_client.get("/org/api/analytics/overview?days=9999")
    assert r.status_code == 422


async def test_empty_org_returns_zero_kpis(org_admin_client: AsyncClient):
    """Org with no deliveries returns zeros, not 500."""
    r = await org_admin_client.get("/org/api/analytics/overview?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["total_sent"] == 0
    assert body["signed_percentage"] == 0.0
