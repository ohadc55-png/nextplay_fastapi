"""Tests for the PlayerContact ORM model + EncryptedText TypeDecorator.

The point: when we write a PlayerContact via SQLAlchemy, the *raw cell value*
that lands in SQLite must be ciphertext (opaque, not the plaintext substring).
On read via the ORM the value is automatically decrypted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.organizations import Organization
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.teams import TeamProfile
from src.models.users import User

pytestmark = pytest.mark.asyncio


async def _seed_player(db_session: AsyncSession) -> tuple[Player, Organization]:
    """Minimum fixture: org + user (coach) + team + player. Returns
    (player, org)."""
    user = User(email="seed@nextplay.test", password_hash="x", display_name="Seed")
    db_session.add(user)
    await db_session.flush()

    org = Organization(slug="encrypt-test", name="Encrypt Test Org")
    db_session.add(org)
    await db_session.flush()

    team = TeamProfile(
        user_id=user.id, team_name="Test Team", organization_id=org.id
    )
    db_session.add(team)
    await db_session.flush()

    player = Player(
        user_id=user.id,
        team_id=team.id,
        organization_id=org.id,
        name="Test Player",
    )
    db_session.add(player)
    await db_session.flush()
    return player, org


async def test_contact_round_trip(db_session: AsyncSession):
    """Write encrypted fields via the ORM, read them back, plaintext matches."""
    player, org = await _seed_player(db_session)

    contact = PlayerContact(
        player_id=player.id,
        organization_id=org.id,
        parent_name="John Smith",
        parent_email="john@example.com",
        parent_phone_enc="+972-50-1234567",
        national_id_enc="123456789",
        medical_notes_enc="Asthma — uses inhaler",
        address_enc="123 Main St, Tel Aviv",
    )
    db_session.add(contact)
    await db_session.flush()
    await db_session.commit()

    # Re-fetch via ORM — values should decrypt automatically.
    fetched = (
        await db_session.execute(
            select(PlayerContact).where(PlayerContact.player_id == player.id)
        )
    ).scalar_one()

    assert fetched.parent_name == "John Smith"
    assert fetched.parent_email == "john@example.com"
    assert fetched.parent_phone_enc == "+972-50-1234567"
    assert fetched.national_id_enc == "123456789"
    assert fetched.medical_notes_enc == "Asthma — uses inhaler"
    assert fetched.address_enc == "123 Main St, Tel Aviv"


async def test_raw_storage_is_opaque(db_session: AsyncSession):
    """Raw SQL read of the encrypted columns returns ciphertext, NOT plaintext.
    This is the security guarantee: a DB dump leaks nothing useful."""
    player, org = await _seed_player(db_session)

    contact = PlayerContact(
        player_id=player.id,
        organization_id=org.id,
        parent_phone_enc="+972-50-7654321",
        national_id_enc="987654321",
        medical_notes_enc="No allergies",
        address_enc="42 Demo St",
    )
    db_session.add(contact)
    await db_session.flush()
    await db_session.commit()

    # Bypass the ORM/TypeDecorator — read the raw stored TEXT.
    raw = (
        await db_session.execute(
            text(
                "SELECT parent_phone_enc, national_id_enc, "
                "medical_notes_enc, address_enc "
                "FROM player_contacts WHERE player_id = :pid"
            ),
            {"pid": player.id},
        )
    ).one()
    raw_phone, raw_id, raw_med, raw_addr = raw

    # The stored cell must NOT contain any plaintext substring.
    for cell, plaintext in (
        (raw_phone, "+972-50-7654321"),
        (raw_phone, "7654321"),
        (raw_id, "987654321"),
        (raw_med, "No allergies"),
        (raw_addr, "42 Demo St"),
    ):
        assert plaintext not in cell, (
            f"plaintext leaked into stored cell: {plaintext!r} found in {cell!r}"
        )

    # Cells should be ASCII (Fernet ciphertext is url-safe base64 + a
    # version byte; it's reasonably long).
    for cell in (raw_phone, raw_id, raw_med, raw_addr):
        assert len(cell) > 30  # Fernet adds ~75 chars of overhead


async def test_null_passthrough(db_session: AsyncSession):
    """A contact row with no encrypted fields stores SQL NULLs, not
    encrypted-empty-strings (which would still be observable)."""
    player, org = await _seed_player(db_session)

    contact = PlayerContact(
        player_id=player.id,
        organization_id=org.id,
        parent_name="Jane",
        # no encrypted fields set
    )
    db_session.add(contact)
    await db_session.flush()
    await db_session.commit()

    raw = (
        await db_session.execute(
            text(
                "SELECT parent_phone_enc, national_id_enc "
                "FROM player_contacts WHERE player_id = :pid"
            ),
            {"pid": player.id},
        )
    ).one()
    assert raw[0] is None
    assert raw[1] is None
