"""Phase 2 closeout — performance benchmark (Part B §10.4 + §11).

Target from the spec: "Send to 3,000 recipients → all delivered within
10 minutes."

What this test actually measures: the latency of the synchronous portion
of `POST /org/api/document-campaigns` for a 3,000-recipient send. That
covers:
  - recipient resolution (3,000 player + contact pairs)
  - one DocumentCampaign row
  - 3,000 DocumentDelivery rows inserted in one transaction
  - 3,000 BackgroundTasks enqueued

What it does NOT measure: the actual external SMS/email dispatch time —
those are provider-bound and out of our control. The dispatch worker is
mocked to a no-op so we measure only what we can optimise.

Marked `slow` so the default `pytest -q` keeps running fast. Trigger via:

    pytest -m slow tests/api/test_bulk_send_perf.py -v

Threshold: the create+dispatch must finish in under **60 seconds** in
CI (generous for SQLite + 3,000-row insert). The 10-minute spec target
includes provider dispatch time we don't measure here.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.models.document_deliveries import DocumentDelivery
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.teams import TeamProfile

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]


RECIPIENT_COUNT = 3000
LATENCY_BUDGET_SECONDS = 60.0  # synchronous portion only


async def _seed_template_and_n_players(
    client: AsyncClient, session_factory, *, n: int,
) -> dict:
    """Create a template + N players + N contacts. Inserted in one
    transaction so seeding itself doesn't dominate the test runtime."""
    with patch("src.services.document_template_service.s3.put_bytes",
               new=AsyncMock(return_value=None)):
        r = await client.post(
            "/org/api/document-templates",
            files={"file": ("t.pdf", b"%PDF-1.4\n" + b"x" * 100, "application/pdf")},
            data={"name": "Perf bench", "category": "OTHER", "requires_signature": "true"},
        )
        assert r.status_code == 201, r.text
        tid = r.json()["id"]

    org_id = client.org_seed["organization_id"]
    coach_id = client.org_seed["user_id"]

    async with session_factory() as s:
        team = TeamProfile(user_id=coach_id, organization_id=org_id, team_name="Bench U12")
        s.add(team)
        await s.flush()

        players = []
        for i in range(n):
            players.append(Player(
                user_id=coach_id, team_id=team.id, organization_id=org_id,
                name=f"Player {i:05d}", active=True,
            ))
        s.add_all(players)
        await s.flush()

        contacts = []
        for p in players:
            contacts.append(PlayerContact(
                player_id=p.id, organization_id=org_id,
                parent_name=f"Parent {p.id}",
                parent_email=f"p{p.id}@example.com",
                parent_phone_enc=f"050-{p.id:07d}",
            ))
        s.add_all(contacts)
        await s.commit()
    return {"template_id": tid}


async def test_bulk_send_3000_recipients_under_budget(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Synchronous send latency for a 3000-recipient campaign stays under
    the budget. Dispatch workers are mocked to a no-op."""
    seed = await _seed_template_and_n_players(
        org_admin_client, api_session_factory, n=RECIPIENT_COUNT,
    )

    with patch("src.api.org_document_campaigns.dispatch_delivery",
               new=AsyncMock()) as dispatch:
        t0 = time.perf_counter()
        r = await org_admin_client.post(
            "/org/api/document-campaigns",
            json={
                "template_id": seed["template_id"],
                "title": f"Perf bench {RECIPIENT_COUNT}",
                "recipient_filter": {"type": "all"},
                "delivery_channels": ["sms"],
            },
        )
        elapsed = time.perf_counter() - t0

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total_recipients"] == RECIPIENT_COUNT
    assert body["status"] == "SENDING"

    # All N deliveries materialized + queued for dispatch.
    async with api_session_factory() as s:
        count = (await s.execute(
            select(DocumentDelivery).where(
                DocumentDelivery.campaign_id == body["id"]
            )
        )).scalars().all()
        assert len(count) == RECIPIENT_COUNT

    assert dispatch.call_count == RECIPIENT_COUNT, (
        f"expected {RECIPIENT_COUNT} dispatch calls, got {dispatch.call_count}"
    )

    # Report — visible with pytest -v even when assertion passes.
    rate = RECIPIENT_COUNT / elapsed if elapsed > 0 else float("inf")
    print(f"\n[perf] {RECIPIENT_COUNT} recipients in {elapsed:.2f}s "
          f"({rate:.0f} deliveries/sec)")

    assert elapsed < LATENCY_BUDGET_SECONDS, (
        f"3000-recipient send took {elapsed:.2f}s, budget is "
        f"{LATENCY_BUDGET_SECONDS}s"
    )
