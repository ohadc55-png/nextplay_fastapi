"""Phase 2.1 smoke tests — ORM-level only.

Goal: verify that every new model is registered, that defaults land where
they should, and that a delivery row can hang off a campaign hanging off a
template hanging off an org. No endpoint logic here — that's 2.2 / 2.3.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document_campaigns import DocumentCampaign
from src.models.document_deliveries import DocumentDelivery
from src.models.document_templates import DocumentTemplate
from src.models.messages import Message, MessageDelivery
from src.models.organizations import Organization
from src.models.otp_attempts import OTPAttempt
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.teams import TeamProfile
from src.models.users import User

pytestmark = pytest.mark.asyncio


async def _make_org_and_player(
    db: AsyncSession,
) -> tuple[Organization, Player, PlayerContact]:
    """Minimal scaffolding so the Phase 2 rows have something to FK to."""
    user = User(
        email="phase2-admin@example.com",
        password_hash="x",
        display_name="Phase 2 Admin",
        email_verified=True,
    )
    db.add(user)
    await db.flush()
    org = Organization(slug="phase2-test", name="Phase 2 Org")
    db.add(org)
    await db.flush()
    team = TeamProfile(
        user_id=user.id,
        organization_id=org.id,
        team_name="Test Team",
    )
    db.add(team)
    await db.flush()
    player = Player(
        user_id=user.id,
        team_id=team.id,
        organization_id=org.id,
        name="Demo Player",
    )
    db.add(player)
    await db.flush()
    contact = PlayerContact(
        player_id=player.id,
        organization_id=org.id,
        parent_name="Demo Parent",
        parent_email="parent@example.com",
        parent_phone_enc="050-1234567",
    )
    db.add(contact)
    await db.flush()
    return org, player, contact


async def test_document_template_defaults(db_session: AsyncSession) -> None:
    org, _, _ = await _make_org_and_player(db_session)
    tpl = DocumentTemplate(
        organization_id=org.id,
        name="הצהרת בריאות 2026",
        category="HEALTH",
        uploaded_file_url="org_1/templates/abc.pdf",
        uploaded_file_type="PDF",
        uploaded_file_size=12345,
    )
    db_session.add(tpl)
    await db_session.flush()
    await db_session.refresh(tpl)
    assert tpl.id is not None
    # Server defaults must kick in:
    assert tpl.requires_signature is True
    assert tpl.is_active is True
    assert tpl.default_expiry_days == 30
    assert tpl.created_at is not None
    assert tpl.updated_at is not None


async def test_document_campaign_with_filter_roundtrips_through_jsontext(
    db_session: AsyncSession,
) -> None:
    org, _, _ = await _make_org_and_player(db_session)
    tpl = DocumentTemplate(
        organization_id=org.id,
        name="t",
        category="OTHER",
        uploaded_file_url="k",
        uploaded_file_type="PDF",
        uploaded_file_size=1,
    )
    db_session.add(tpl)
    await db_session.flush()

    expires = datetime.now(UTC) + timedelta(days=30)
    campaign = DocumentCampaign(
        organization_id=org.id,
        template_id=tpl.id,
        title="ברודקאסט בריאות",
        recipient_filter={"type": "team", "team_ids": [1, 2, 3]},
        delivery_channels=["email", "sms"],
        expires_at=expires,
    )
    db_session.add(campaign)
    await db_session.flush()
    await db_session.refresh(campaign)
    # JSONText round-trips dict + list seamlessly.
    assert campaign.recipient_filter == {"type": "team", "team_ids": [1, 2, 3]}
    assert campaign.delivery_channels == ["email", "sms"]
    # Status default.
    assert campaign.status == "DRAFT"
    assert campaign.total_recipients == 0


async def test_document_delivery_token_lookup(db_session: AsyncSession) -> None:
    """The token index is the hot path for the public /sign/{token} route."""
    org, player, contact = await _make_org_and_player(db_session)
    tpl = DocumentTemplate(
        organization_id=org.id, name="t", category="OTHER",
        uploaded_file_url="k", uploaded_file_type="PDF", uploaded_file_size=1,
    )
    db_session.add(tpl)
    await db_session.flush()
    campaign = DocumentCampaign(
        organization_id=org.id, template_id=tpl.id, title="t",
        recipient_filter={"type": "all"},
        delivery_channels=["sms"],
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(campaign)
    await db_session.flush()

    token = "abc123" * 5 + "ab"  # arbitrary 32-char-ish unique string
    delivery = DocumentDelivery(
        campaign_id=campaign.id,
        organization_id=org.id,
        player_id=player.id,
        player_contact_id=contact.id,
        recipient_name="Demo Parent",
        recipient_email="parent@example.com",
        recipient_phone="050-1234567",
        unique_token=token,
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(delivery)
    await db_session.flush()
    await db_session.refresh(delivery)

    # Defaults
    assert delivery.delivery_status == "PENDING"
    assert delivery.document_status == "NOT_OPENED"
    assert delivery.reminders_sent_count == 0

    # Token lookup
    found = (
        await db_session.execute(
            select(DocumentDelivery).where(DocumentDelivery.unique_token == token)
        )
    ).scalar_one()
    assert found.id == delivery.id


async def test_otp_attempt_defaults(db_session: AsyncSession) -> None:
    org, _, _ = await _make_org_and_player(db_session)
    otp = OTPAttempt(
        organization_id=org.id,
        delivery_token="t" * 32,
        phone="050-1234567",
        code_hash="x" * 64,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db_session.add(otp)
    await db_session.flush()
    await db_session.refresh(otp)
    assert otp.attempts == 0
    assert otp.max_attempts == 3
    assert otp.verified_at is None


async def test_message_and_delivery_pair(db_session: AsyncSession) -> None:
    org, player, contact = await _make_org_and_player(db_session)
    msg = Message(
        organization_id=org.id,
        subject="תזכורת אימון",
        body="האימון נדחה ל-19:00",
        recipient_filter={"type": "team", "team_ids": [1]},
        delivery_channels=["sms"],
    )
    db_session.add(msg)
    await db_session.flush()
    md = MessageDelivery(
        message_id=msg.id,
        organization_id=org.id,
        player_id=player.id,
        player_contact_id=contact.id,
        recipient_phone="050-1234567",
    )
    db_session.add(md)
    await db_session.flush()
    await db_session.refresh(msg)
    await db_session.refresh(md)
    assert msg.status == "DRAFT"
    assert md.delivery_status == "PENDING"
