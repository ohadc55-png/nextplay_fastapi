"""End-to-end tests for the public signing flow — Phase 2.3.

We seed an Organization + Player + DocumentTemplate + DocumentCampaign +
DocumentDelivery + (mocked) S3 template bytes directly via the session,
then drive the public router with the httpx test client (no auth cookies
required — these endpoints live outside the org/admin sessions).

All S3 calls are mocked at the module boundary; PyMuPDF runs for real on
the test PDF bytes (it's pure-Python wheels and fast).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.models.document_campaigns import DocumentCampaign
from src.models.document_deliveries import DocumentDelivery
from src.models.document_templates import DocumentTemplate
from src.models.org_audit import OrgAuditLog
from src.models.organizations import Organization
from src.models.otp_attempts import OTPAttempt
from src.models.players import Player
from src.models.teams import TeamProfile
from src.models.users import User

pytestmark = pytest.mark.asyncio


# Minimal PDF that PyMuPDF can parse + paint on.
_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<<>>>>endobj\n"
    b"xref\n0 4\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000054 00000 n \n"
    b"0000000101 00000 n \n"
    b"trailer<</Root 1 0 R/Size 4>>\n"
    b"startxref\n166\n%%EOF"
)


async def _seed(session_factory, *, requires_signature: bool = True) -> dict:
    """Build a complete (org, template, campaign, delivery, token) scaffold."""
    async with session_factory() as s:
        admin = User(email="admin@org.test", password_hash="x", display_name="A", email_verified=True)
        s.add(admin)
        await s.flush()
        org = Organization(slug="public-sign-test", name="Test Org")
        s.add(org)
        await s.flush()
        team = TeamProfile(user_id=admin.id, organization_id=org.id, team_name="Team")
        s.add(team)
        await s.flush()
        player = Player(user_id=admin.id, team_id=team.id, organization_id=org.id, name="Dana")
        s.add(player)
        await s.flush()

        tpl = DocumentTemplate(
            organization_id=org.id,
            name="Health Form",
            category="HEALTH",
            uploaded_file_url="org_1/templates/test.pdf",
            uploaded_file_type="PDF",
            uploaded_file_size=len(_MINIMAL_PDF),
            requires_signature=requires_signature,
            form_fields=[
                {"id": "f1", "type": "text", "label": "Name",
                 "required": True, "x": 100, "y": 200, "width": 300, "height": 30, "page": 1},
            ],
            signature_zones=([
                {"id": "s1", "label": "Sig",
                 "x": 100, "y": 700, "width": 200, "height": 50, "page": 1}
            ] if requires_signature else []),
        )
        s.add(tpl)
        await s.flush()

        now = datetime.now(UTC).replace(tzinfo=None)
        camp = DocumentCampaign(
            organization_id=org.id,
            template_id=tpl.id,
            title="Q3 Send",
            recipient_filter={"type": "all"},
            delivery_channels=["sms"],
            expires_at=now + timedelta(days=30),
        )
        s.add(camp)
        await s.flush()

        token = uuid.uuid4().hex
        delivery = DocumentDelivery(
            campaign_id=camp.id,
            organization_id=org.id,
            player_id=player.id,
            recipient_name="Ruth Parent",
            recipient_email="parent@example.com",
            recipient_phone="050-1234567",
            unique_token=token,
            expires_at=now + timedelta(days=30),
        )
        s.add(delivery)
        await s.commit()

        return {
            "org_id": org.id,
            "player_id": player.id,
            "template_id": tpl.id,
            "campaign_id": camp.id,
            "delivery_id": delivery.id,
            "token": token,
        }


# ---------------------------------------------------------------------------
# Token / state-machine guards
# ---------------------------------------------------------------------------


async def test_unknown_token_returns_404_html(api_client: AsyncClient):
    r = await api_client.get("/sign/this-is-not-a-real-token")
    assert r.status_code == 404
    assert "html" in r.headers["content-type"]
    # Generic message, doesn't leak existence.
    assert "לא תקין" in r.text or "Not" in r.text


async def test_expired_token_returns_410(
    api_client: AsyncClient, api_session_factory,
):
    seed = await _seed(api_session_factory)
    # Push the delivery into the past.
    async with api_session_factory() as s:
        d = await s.get(DocumentDelivery, seed["delivery_id"])
        d.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
        await s.commit()

    r = await api_client.get(f"/sign/{seed['token']}")
    assert r.status_code == 410


async def test_first_get_flips_status_and_writes_audit(
    api_client: AsyncClient, api_session_factory,
):
    seed = await _seed(api_session_factory)
    r = await api_client.get(f"/sign/{seed['token']}")
    assert r.status_code == 200

    async with api_session_factory() as s:
        d = await s.get(DocumentDelivery, seed["delivery_id"])
        assert d.document_status == "OPENED"
        assert d.opened_at is not None
        audits = (await s.execute(
            select(OrgAuditLog).where(OrgAuditLog.action == "document.opened")
        )).scalars().all()
        assert len(audits) == 1


async def test_already_signed_renders_dedicated_page(
    api_client: AsyncClient, api_session_factory,
):
    seed = await _seed(api_session_factory)
    async with api_session_factory() as s:
        d = await s.get(DocumentDelivery, seed["delivery_id"])
        d.document_status = "SIGNED"
        d.signed_at = datetime.now(UTC).replace(tzinfo=None)
        await s.commit()

    r = await api_client.get(f"/sign/{seed['token']}")
    assert r.status_code == 200
    assert "כבר נחתם" in r.text


# ---------------------------------------------------------------------------
# OTP flow
# ---------------------------------------------------------------------------


async def test_otp_request_wrong_phone_returns_404(
    api_client: AsyncClient, api_session_factory,
):
    seed = await _seed(api_session_factory)
    with patch("src.api.public_sign.get_sms_provider") as factory:
        provider = AsyncMock()
        provider.send = AsyncMock()
        factory.return_value = provider
        r = await api_client.post(
            f"/sign/{seed['token']}/otp/request", json={"phone": "999-9999999"}
        )
    assert r.status_code == 404
    # SMS provider should NOT have been called.
    assert provider.send.await_count == 0


async def test_otp_request_correct_phone_sends_sms(
    api_client: AsyncClient, api_session_factory,
):
    seed = await _seed(api_session_factory)
    with patch("src.api.public_sign.get_sms_provider") as factory:
        provider = AsyncMock()
        provider.send = AsyncMock()
        factory.return_value = provider
        r = await api_client.post(
            f"/sign/{seed['token']}/otp/request", json={"phone": "050-1234567"}
        )
    assert r.status_code == 200, r.text
    assert provider.send.await_count == 1
    sent_phone, sent_body = provider.send.call_args.args
    assert sent_phone == "0501234567"  # normalized
    assert "קוד אימות" in sent_body

    async with api_session_factory() as s:
        otps = (await s.execute(select(OTPAttempt))).scalars().all()
        assert len(otps) == 1
        assert otps[0].code_hash and len(otps[0].code_hash) == 64
        assert otps[0].phone == "0501234567"


async def test_otp_rate_limited_after_three_requests(
    api_client: AsyncClient, api_session_factory,
):
    seed = await _seed(api_session_factory)
    with patch("src.api.public_sign.get_sms_provider") as factory:
        provider = AsyncMock()
        provider.send = AsyncMock()
        factory.return_value = provider
        for _ in range(3):
            r = await api_client.post(
                f"/sign/{seed['token']}/otp/request", json={"phone": "050-1234567"}
            )
            assert r.status_code == 200
        # 4th request → 429
        r = await api_client.post(
            f"/sign/{seed['token']}/otp/request", json={"phone": "050-1234567"}
        )
        assert r.status_code == 429


async def test_otp_verify_invalid_then_valid(
    api_client: AsyncClient, api_session_factory,
):
    seed = await _seed(api_session_factory)
    # Generate a known OTP by patching secrets at issue time.
    with patch("src.api.public_sign.get_sms_provider") as factory, \
         patch("src.api.public_sign._generate_otp_code", return_value="123456"):
        provider = AsyncMock()
        provider.send = AsyncMock()
        factory.return_value = provider
        r = await api_client.post(
            f"/sign/{seed['token']}/otp/request", json={"phone": "050-1234567"}
        )
        assert r.status_code == 200

    # Wrong code
    r = await api_client.post(
        f"/sign/{seed['token']}/otp/verify", json={"code": "000000"}
    )
    assert r.status_code == 400

    # Right code
    r = await api_client.post(
        f"/sign/{seed['token']}/otp/verify", json={"code": "123456"}
    )
    assert r.status_code == 200, r.text
    assert r.cookies.get("signing_session"), r.cookies


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


async def _verified_client(
    api_client: AsyncClient, api_session_factory, *, requires_signature: bool = True,
) -> tuple[AsyncClient, dict]:
    """Drive the full OTP flow so api_client carries the signing_session cookie."""
    seed = await _seed(api_session_factory, requires_signature=requires_signature)
    if not requires_signature:
        return api_client, seed
    with patch("src.api.public_sign.get_sms_provider") as factory, \
         patch("src.api.public_sign._generate_otp_code", return_value="123456"):
        provider = AsyncMock()
        provider.send = AsyncMock()
        factory.return_value = provider
        r = await api_client.post(
            f"/sign/{seed['token']}/otp/request", json={"phone": "050-1234567"}
        )
        assert r.status_code == 200
    r = await api_client.post(
        f"/sign/{seed['token']}/otp/verify", json={"code": "123456"}
    )
    assert r.status_code == 200, r.text
    return api_client, seed


async def test_submit_without_session_returns_404(
    api_client: AsyncClient, api_session_factory,
):
    seed = await _seed(api_session_factory)
    r = await api_client.post(
        f"/sign/{seed['token']}/submit",
        json={
            "form_response": {"f1": "Dana Cohen"},
            "signature_method": "TYPED",
            "typed_signature": "Ruth Cohen",
        },
    )
    assert r.status_code == 404


async def test_submit_with_typed_signature_signs_delivery(
    api_client: AsyncClient, api_session_factory,
):
    client, seed = await _verified_client(api_client, api_session_factory)
    with patch("src.services.pdf_generation_service.s3.get_bytes",
               new=AsyncMock(return_value=_MINIMAL_PDF)), \
         patch("src.services.pdf_generation_service.s3.put_bytes",
               new=AsyncMock(return_value=None)) as put_mock:
        r = await client.post(
            f"/sign/{seed['token']}/submit",
            json={
                "form_response": {"f1": "Dana Cohen"},
                "signature_method": "TYPED",
                "typed_signature": "Ruth Cohen",
            },
        )
    assert r.status_code == 200, r.text
    # S3 was called with the final PDF.
    assert put_mock.await_count == 1
    final_key = put_mock.await_args.kwargs["key"]
    assert final_key.endswith(f"/signed/{seed['delivery_id']}.pdf")

    async with api_session_factory() as s:
        d = await s.get(DocumentDelivery, seed["delivery_id"])
        assert d.document_status == "SIGNED"
        assert d.signature_method == "TYPED"
        assert d.final_pdf_url == final_key
        assert d.form_response == {"f1": "Dana Cohen"}
        assert d.audit_data and "payload_hash" in d.audit_data


async def test_submit_rejects_missing_required_field(
    api_client: AsyncClient, api_session_factory,
):
    client, seed = await _verified_client(api_client, api_session_factory)
    r = await client.post(
        f"/sign/{seed['token']}/submit",
        json={
            "form_response": {},  # f1 is required, missing
            "signature_method": "TYPED",
            "typed_signature": "Ruth",
        },
    )
    assert r.status_code == 422, r.text
    assert r.json().get("code") == "missing_required_field"


async def test_resubmit_on_signed_delivery_returns_404(
    api_client: AsyncClient, api_session_factory,
):
    client, seed = await _verified_client(api_client, api_session_factory)
    with patch("src.services.pdf_generation_service.s3.get_bytes",
               new=AsyncMock(return_value=_MINIMAL_PDF)), \
         patch("src.services.pdf_generation_service.s3.put_bytes",
               new=AsyncMock(return_value=None)):
        r = await client.post(
            f"/sign/{seed['token']}/submit",
            json={
                "form_response": {"f1": "Dana"},
                "signature_method": "TYPED",
                "typed_signature": "Ruth",
            },
        )
        assert r.status_code == 200
        # Second attempt — already SIGNED.
        r2 = await client.post(
            f"/sign/{seed['token']}/submit",
            json={
                "form_response": {"f1": "Dana"},
                "signature_method": "TYPED",
                "typed_signature": "Ruth",
            },
        )
    assert r2.status_code == 404


async def test_complete_page_renders_after_signing(
    api_client: AsyncClient, api_session_factory,
):
    client, seed = await _verified_client(api_client, api_session_factory)
    with patch("src.services.pdf_generation_service.s3.get_bytes",
               new=AsyncMock(return_value=_MINIMAL_PDF)), \
         patch("src.services.pdf_generation_service.s3.put_bytes",
               new=AsyncMock(return_value=None)):
        await client.post(
            f"/sign/{seed['token']}/submit",
            json={
                "form_response": {"f1": "Dana"},
                "signature_method": "TYPED",
                "typed_signature": "Ruth",
            },
        )

    r = await client.get(f"/sign/{seed['token']}/complete")
    assert r.status_code == 200
    assert "תודה" in r.text
