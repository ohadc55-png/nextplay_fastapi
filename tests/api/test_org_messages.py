"""Endpoint tests for /org/api/messages/* — Phase 2.5.

Covers:
- preview-recipients count
- create + send (creates one MessageDelivery per recipient + queues dispatch)
- save-as-draft (no deliveries created, status=DRAFT)
- patch draft + send-draft
- delete draft
- list + status filter
- cross-org isolation
- placeholder rendering
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.models.messages import Message, MessageDelivery
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.teams import TeamProfile

pytestmark = pytest.mark.asyncio


async def _seed_players(client: AsyncClient, session_factory) -> dict:
    org_id = client.org_seed["organization_id"]
    coach_id = client.org_seed["user_id"]

    async with session_factory() as s:
        team = TeamProfile(user_id=coach_id, organization_id=org_id, team_name="U12")
        s.add(team)
        await s.flush()
        ids: list[int] = []
        for i in range(3):
            p = Player(
                user_id=coach_id, team_id=team.id, organization_id=org_id,
                name=f"Kid {i}", active=True,
            )
            s.add(p)
            await s.flush()
            s.add(PlayerContact(
                player_id=p.id, organization_id=org_id,
                parent_name=f"Parent {i}",
                parent_email=f"p{i}@example.com",
                parent_phone_enc=f"050-100000{i}",
            ))
            ids.append(p.id)
        await s.commit()
    return {"team_id": team.id, "player_ids": ids}


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


async def test_preview_recipients_counts_active_org_players(
    org_admin_client: AsyncClient, api_session_factory,
):
    await _seed_players(org_admin_client, api_session_factory)
    r = await org_admin_client.post(
        "/org/api/messages/preview-recipients",
        json={"recipient_filter": {"type": "all"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3


# ---------------------------------------------------------------------------
# Create + send
# ---------------------------------------------------------------------------


async def test_create_message_creates_one_delivery_per_recipient(
    org_admin_client: AsyncClient, api_session_factory,
):
    await _seed_players(org_admin_client, api_session_factory)
    with patch("src.api.org_messages.dispatch_message_delivery",
               new=AsyncMock()) as dispatch:
        r = await org_admin_client.post(
            "/org/api/messages",
            json={
                "subject": "Reminder",
                "body": "Practice at 18:00. Hi {{parent_name}}.",
                "recipient_filter": {"type": "all"},
                "delivery_channels": ["sms", "email"],
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "SENDING"
    assert body["total_recipients"] == 3

    async with api_session_factory() as s:
        rows = (await s.execute(select(MessageDelivery))).scalars().all()
        assert len(rows) == 3
        # The phone snapshot is the (mock-encrypted) plaintext.
        assert all(r.recipient_phone and r.recipient_phone.startswith("050-") for r in rows)
    assert dispatch.call_count == 3


async def test_save_as_draft_creates_no_deliveries(
    org_admin_client: AsyncClient, api_session_factory,
):
    await _seed_players(org_admin_client, api_session_factory)
    r = await org_admin_client.post(
        "/org/api/messages",
        json={
            "subject": "Draft",
            "body": "still thinking",
            "recipient_filter": {"type": "all"},
            "delivery_channels": ["email"],
            "save_as_draft": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "DRAFT"

    async with api_session_factory() as s:
        msgs = (await s.execute(select(Message))).scalars().all()
        delivs = (await s.execute(select(MessageDelivery))).scalars().all()
    assert len(msgs) == 1
    assert msgs[0].status == "DRAFT"
    assert delivs == []


# ---------------------------------------------------------------------------
# Promote draft → send
# ---------------------------------------------------------------------------


async def test_promote_draft_creates_deliveries(
    org_admin_client: AsyncClient, api_session_factory,
):
    await _seed_players(org_admin_client, api_session_factory)
    r = await org_admin_client.post(
        "/org/api/messages",
        json={
            "subject": "D",
            "body": "x",
            "recipient_filter": {"type": "all"},
            "delivery_channels": ["sms"],
            "save_as_draft": True,
        },
    )
    msg_id = r.json()["id"]

    with patch("src.api.org_messages.dispatch_message_delivery",
               new=AsyncMock()) as dispatch:
        r = await org_admin_client.post(f"/org/api/messages/{msg_id}/send")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "SENDING"
    assert dispatch.call_count == 3


# ---------------------------------------------------------------------------
# List + status filter
# ---------------------------------------------------------------------------


async def test_list_with_status_filter(
    org_admin_client: AsyncClient, api_session_factory,
):
    await _seed_players(org_admin_client, api_session_factory)
    # 1 draft + 1 sent
    await org_admin_client.post(
        "/org/api/messages",
        json={"subject": "Dr", "body": "x", "recipient_filter": {"type": "all"},
              "delivery_channels": ["sms"], "save_as_draft": True},
    )
    with patch("src.api.org_messages.dispatch_message_delivery", new=AsyncMock()):
        await org_admin_client.post(
            "/org/api/messages",
            json={"subject": "Sent", "body": "x", "recipient_filter": {"type": "all"},
                  "delivery_channels": ["sms"]},
        )

    r = await org_admin_client.get("/org/api/messages?status_filter=DRAFT")
    assert r.status_code == 200
    assert len(r.json()["messages"]) == 1
    assert r.json()["messages"][0]["subject"] == "Dr"

    r = await org_admin_client.get("/org/api/messages")
    assert r.status_code == 200
    assert len(r.json()["messages"]) == 2


# ---------------------------------------------------------------------------
# Cross-org isolation
# ---------------------------------------------------------------------------


async def test_get_message_cross_org_404(
    org_admin_client: AsyncClient, api_session_factory, seed_org_admin, api_client,
):
    await _seed_players(org_admin_client, api_session_factory)
    r = await org_admin_client.post(
        "/org/api/messages",
        json={"subject": "Mine", "body": "x", "recipient_filter": {"type": "all"},
              "delivery_channels": ["sms"], "save_as_draft": True},
    )
    msg_id = r.json()["id"]

    # Switch session to another org.
    await api_client.post("/org/logout")
    other = await seed_org_admin(email="b@org.test", org_slug="other", org_name="Other")
    await api_client.post(
        "/org/login", json={"email": other["email"], "password": other["password"]}
    )
    r = await api_client.get(f"/org/api/messages/{msg_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Delete draft
# ---------------------------------------------------------------------------


async def test_delete_draft(
    org_admin_client: AsyncClient, api_session_factory,
):
    await _seed_players(org_admin_client, api_session_factory)
    r = await org_admin_client.post(
        "/org/api/messages",
        json={"subject": "Bin me", "body": "x", "recipient_filter": {"type": "all"},
              "delivery_channels": ["sms"], "save_as_draft": True},
    )
    msg_id = r.json()["id"]
    r = await org_admin_client.delete(f"/org/api/messages/{msg_id}")
    assert r.status_code == 200
    r = await org_admin_client.get(f"/org/api/messages/{msg_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Placeholder rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio  # noqa: ASYNCIO_MARK_PRESERVED
async def test_render_placeholders_substitutes_known_keys():
    from src.services.message_service import render_placeholders

    out = render_placeholders(
        "Hi {{parent_name}}, your kid {{player_name}} on {{team_name}}.",
        values={"parent_name": "Dana", "player_name": "Yossi", "team_name": "U12"},
    )
    assert out == "Hi Dana, your kid Yossi on U12."


@pytest.mark.asyncio  # noqa: ASYNCIO_MARK_PRESERVED
async def test_render_placeholders_leaves_unknown_keys_alone():
    from src.services.message_service import render_placeholders

    out = render_placeholders("Hi {{stranger}}", values={"parent_name": "X"})
    # Unknown keys aren't in PLACEHOLDER_KEYS, so they survive untouched.
    assert out == "Hi {{stranger}}"


# ---------------------------------------------------------------------------
# Phase 2.6c — scheduled messages
# ---------------------------------------------------------------------------


async def test_create_message_with_future_scheduled_at_stays_scheduled(
    org_admin_client: AsyncClient, api_session_factory,
):
    """scheduled_at in the future → status=SCHEDULED, no deliveries created."""
    from datetime import UTC, datetime, timedelta

    await _seed_players(org_admin_client, api_session_factory)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    r = await org_admin_client.post(
        "/org/api/messages",
        json={
            "subject": "Future message",
            "body": "Hi {{parent_name}}",
            "recipient_filter": {"type": "all"},
            "delivery_channels": ["sms"],
            "scheduled_at": future,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "SCHEDULED"
    assert body["scheduled_at"] is not None

    # No deliveries materialized yet.
    from sqlalchemy import select

    from src.models.messages import MessageDelivery
    async with api_session_factory() as s:
        rows = (await s.execute(select(MessageDelivery))).scalars().all()
    assert rows == []


async def test_scheduled_at_in_past_rejected(
    org_admin_client: AsyncClient, api_session_factory,
):
    from datetime import UTC, datetime, timedelta

    await _seed_players(org_admin_client, api_session_factory)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    r = await org_admin_client.post(
        "/org/api/messages",
        json={
            "subject": "Past",
            "body": "x",
            "recipient_filter": {"type": "all"},
            "delivery_channels": ["sms"],
            "scheduled_at": past,
        },
    )
    assert r.status_code == 422
    assert r.json().get("code") == "schedule_in_past"
