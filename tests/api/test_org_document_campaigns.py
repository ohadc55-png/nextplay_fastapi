"""Endpoint tests for /org/api/document-campaigns/* — Phase 2.4.

Focus: recipient resolution, campaign+deliveries creation, cross-org
isolation, and that BackgroundTasks fire dispatch_delivery for each row.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.models.document_campaigns import DocumentCampaign
from src.models.document_deliveries import DocumentDelivery
from src.models.document_templates import DocumentTemplate
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.teams import TeamProfile

pytestmark = pytest.mark.asyncio


async def _seed_template_and_players(client: AsyncClient, session_factory) -> dict:
    """Create a template (via POST so S3 is mocked) + 3 players w/ contacts
    in the client's active org."""
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
        player_ids = []
        for i in range(3):
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
            player_ids.append(p.id)
        await s.commit()

    return {"template_id": tid, "team_id": team.id, "player_ids": player_ids}


# ---------------------------------------------------------------------------
# Preview count
# ---------------------------------------------------------------------------


async def test_preview_recipients_all_in_org(
    org_admin_client: AsyncClient, api_session_factory,
):
    await _seed_template_and_players(org_admin_client, api_session_factory)
    r = await org_admin_client.post(
        "/org/api/document-campaigns/preview-recipients",
        json={"recipient_filter": {"type": "all"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3


async def test_preview_recipients_specific_players(
    org_admin_client: AsyncClient, api_session_factory,
):
    seed = await _seed_template_and_players(org_admin_client, api_session_factory)
    r = await org_admin_client.post(
        "/org/api/document-campaigns/preview-recipients",
        json={
            "recipient_filter": {
                "type": "specific_players",
                "player_ids": seed["player_ids"][:2],
            },
        },
    )
    assert r.status_code == 200
    assert r.json()["count"] == 2


# ---------------------------------------------------------------------------
# Create + send
# ---------------------------------------------------------------------------


async def test_create_campaign_makes_one_delivery_per_recipient(
    org_admin_client: AsyncClient, api_session_factory,
):
    seed = await _seed_template_and_players(org_admin_client, api_session_factory)
    with patch("src.api.org_document_campaigns.dispatch_delivery",
               new=AsyncMock()) as dispatch:
        r = await org_admin_client.post(
            "/org/api/document-campaigns",
            json={
                "template_id": seed["template_id"],
                "title": "Q4 health",
                "recipient_filter": {"type": "all"},
                "delivery_channels": ["sms", "email"],
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total_recipients"] == 3
    assert body["status"] == "SENDING"

    # 3 deliveries, each with a unique token.
    async with api_session_factory() as s:
        rows = (await s.execute(select(DocumentDelivery))).scalars().all()
        assert len(rows) == 3
        tokens = {row.unique_token for row in rows}
        assert len(tokens) == 3
        # Phone snapshot is decrypted plaintext.
        assert rows[0].recipient_phone and rows[0].recipient_phone.startswith("050-")
    # BackgroundTasks ran dispatch_delivery once per row.
    assert dispatch.call_count == 3


async def test_create_campaign_rejects_empty_filter(
    org_admin_client: AsyncClient, api_session_factory,
):
    seed = await _seed_template_and_players(org_admin_client, api_session_factory)
    r = await org_admin_client.post(
        "/org/api/document-campaigns",
        json={
            "template_id": seed["template_id"],
            "title": "Nope",
            "recipient_filter": {"type": "specific_players", "player_ids": [99999]},
        },
    )
    assert r.status_code == 422
    assert r.json().get("code") == "no_recipients"


async def test_create_campaign_cross_org_template_returns_404(
    org_admin_client: AsyncClient, api_session_factory, seed_org_admin, api_client,
):
    seed = await _seed_template_and_players(org_admin_client, api_session_factory)

    # Switch to a different org.
    await api_client.post("/org/logout")
    other = await seed_org_admin(
        email="b@org.test", org_slug="other-org", org_name="Other"
    )
    r = await api_client.post(
        "/org/login", json={"email": other["email"], "password": other["password"]}
    )
    assert r.status_code == 200

    r = await api_client.post(
        "/org/api/document-campaigns",
        json={
            "template_id": seed["template_id"],
            "title": "Cross",
            "recipient_filter": {"type": "all"},
        },
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------


async def test_list_and_get_campaign(
    org_admin_client: AsyncClient, api_session_factory,
):
    seed = await _seed_template_and_players(org_admin_client, api_session_factory)
    with patch("src.api.org_document_campaigns.dispatch_delivery", new=AsyncMock()):
        r = await org_admin_client.post(
            "/org/api/document-campaigns",
            json={
                "template_id": seed["template_id"],
                "title": "C1",
                "recipient_filter": {"type": "all"},
            },
        )
    cid = r.json()["id"]

    r = await org_admin_client.get("/org/api/document-campaigns")
    assert r.status_code == 200
    items = r.json()["campaigns"]
    assert len(items) == 1
    assert items[0]["title"] == "C1"

    r = await org_admin_client.get(f"/org/api/document-campaigns/{cid}")
    assert r.status_code == 200
    assert r.json()["id"] == cid
