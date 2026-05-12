"""Tests for /org/api/players/{id}/contact (Phase 1.6).

Three things to guard:
1. Decrypted plaintext lands in API responses (round-trip).
2. The raw cell value in storage is opaque (no plaintext leak).
3. Every read AND every write emits a `player.contact.read/write` audit row.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_team_and_player(
    org_admin_client: AsyncClient, api_session_factory, player_name: str = "P",
) -> int:
    from src.models.players import Player
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        t = TeamProfile(organization_id=org_id, team_name="T")
        s.add(t)
        await s.flush()
        p = Player(
            organization_id=org_id, team_id=t.id, name=player_name, active=True,
        )
        s.add(p)
        await s.commit()
        return p.id


# ---------------------------------------------------------------------------
# Read empty contact
# ---------------------------------------------------------------------------


async def test_get_empty_contact_returns_nulls(
    org_admin_client: AsyncClient, api_session_factory,
):
    pid = await _seed_team_and_player(org_admin_client, api_session_factory)
    r = await org_admin_client.get(f"/org/api/players/{pid}/contact")
    assert r.status_code == 200
    body = r.json()
    assert body["player_id"] == pid
    assert body["parent_name"] is None
    assert body["parent_phone"] is None
    assert body["national_id"] is None


async def test_get_empty_contact_still_audits(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Even an empty (no row) read must produce an audit log entry."""
    from src.models.org_audit import OrgAuditLog

    pid = await _seed_team_and_player(org_admin_client, api_session_factory)
    await org_admin_client.get(f"/org/api/players/{pid}/contact")

    async with api_session_factory() as s:
        rows = (await s.execute(
            select(OrgAuditLog).where(
                OrgAuditLog.action == "player.contact.read",
                OrgAuditLog.target_id == pid,
            )
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].attributes_json == {"present": False}


# ---------------------------------------------------------------------------
# Write then read round-trip
# ---------------------------------------------------------------------------


async def test_put_then_get_round_trip(
    org_admin_client: AsyncClient, api_session_factory,
):
    pid = await _seed_team_and_player(org_admin_client, api_session_factory)
    payload = {
        "parent_name": "John Smith",
        "parent_email": "john@example.com",
        "parent_phone": "+972-50-1234567",
        "national_id": "123456789",
        "medical_notes": "Asthma — uses inhaler",
        "address": "1 Demo St, Tel Aviv",
    }
    r_put = await org_admin_client.put(
        f"/org/api/players/{pid}/contact", json=payload,
    )
    assert r_put.status_code == 200

    r_get = await org_admin_client.get(f"/org/api/players/{pid}/contact")
    body = r_get.json()
    for k, v in payload.items():
        assert body[k] == v


# ---------------------------------------------------------------------------
# Storage is opaque (no plaintext leak)
# ---------------------------------------------------------------------------


async def test_storage_is_ciphertext_not_plaintext(
    org_admin_client: AsyncClient, api_session_factory,
):
    pid = await _seed_team_and_player(org_admin_client, api_session_factory)
    await org_admin_client.put(
        f"/org/api/players/{pid}/contact",
        json={
            "parent_phone": "+972-50-7654321",
            "national_id": "987654321",
            "medical_notes": "No allergies",
            "address": "42 Cipher St",
        },
    )

    async with api_session_factory() as s:
        raw = (await s.execute(
            text(
                "SELECT parent_phone_enc, national_id_enc, "
                "medical_notes_enc, address_enc "
                "FROM player_contacts WHERE player_id = :pid"
            ),
            {"pid": pid},
        )).one()
        raw_phone, raw_nid, raw_med, raw_addr = raw

    for cell, plaintext in (
        (raw_phone, "+972-50-7654321"),
        (raw_phone, "7654321"),
        (raw_nid, "987654321"),
        (raw_med, "No allergies"),
        (raw_addr, "42 Cipher St"),
    ):
        assert plaintext not in cell, (
            f"plaintext leaked into stored cell: {plaintext!r} in {cell!r}"
        )


# ---------------------------------------------------------------------------
# Audit log on read + write
# ---------------------------------------------------------------------------


async def test_audit_on_read_and_write(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.org_audit import OrgAuditLog

    pid = await _seed_team_and_player(org_admin_client, api_session_factory)

    await org_admin_client.put(
        f"/org/api/players/{pid}/contact",
        json={"parent_phone": "+972-50-1111111"},
    )
    await org_admin_client.get(f"/org/api/players/{pid}/contact")
    await org_admin_client.get(f"/org/api/players/{pid}/contact")

    async with api_session_factory() as s:
        writes = (await s.execute(
            select(OrgAuditLog).where(
                OrgAuditLog.action == "player.contact.write",
                OrgAuditLog.target_id == pid,
            )
        )).scalars().all()
        reads = (await s.execute(
            select(OrgAuditLog).where(
                OrgAuditLog.action == "player.contact.read",
                OrgAuditLog.target_id == pid,
            )
        )).scalars().all()
        assert len(writes) == 1
        assert len(reads) == 2
        assert writes[0].attributes_json["fields_changed"] == ["parent_phone"]


# ---------------------------------------------------------------------------
# Cross-tenant: no audit row, 404
# ---------------------------------------------------------------------------


async def test_cross_tenant_get_returns_404_and_writes_no_audit(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """A cross-org GET /contact returns 404 BEFORE any audit row is written.
    The audit log must not record events for resources the caller can't see."""
    from src.models.org_audit import OrgAuditLog
    from src.models.players import Player
    from src.models.teams import TeamProfile

    a = await seed_org_admin(email="aaa@x.test", org_slug="aaa-x", org_name="A")
    b = await seed_org_admin(email="bbb@x.test", org_slug="bbb-x", org_name="B")
    async with api_session_factory() as s:
        team = TeamProfile(organization_id=a["organization_id"], team_name="A team")
        s.add(team)
        await s.flush()
        p = Player(
            organization_id=a["organization_id"], team_id=team.id, name="X", active=True,
        )
        s.add(p)
        await s.commit()
        pid_in_a = p.id

    await api_client.post(
        "/org/login", json={"email": b["email"], "password": b["password"]}
    )
    r = await api_client.get(f"/org/api/players/{pid_in_a}/contact")
    assert r.status_code == 404

    async with api_session_factory() as s:
        rows = (await s.execute(
            select(OrgAuditLog).where(
                OrgAuditLog.action == "player.contact.read",
                OrgAuditLog.target_id == pid_in_a,
            )
        )).scalars().all()
        assert rows == []
