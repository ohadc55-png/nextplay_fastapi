"""Tests for /admin/api/orgs/wizard/* (Phase 1.8).

Same admin-session pattern as test_admin_orgs.py.
"""

from __future__ import annotations

from unittest.mock import patch

import bcrypt
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from src.core.config import settings

pytestmark = pytest.mark.asyncio

ADMIN_EMAIL = "admin@wiz.test"
ADMIN_PASSWORD = "AdminPass1"


@pytest_asyncio.fixture
def admin_password_hash() -> str:
    return bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest_asyncio.fixture
async def admin_env(admin_password_hash: str):
    with patch.object(settings, "ADMIN_PASSWORD_HASH", admin_password_hash), \
         patch.object(settings, "ADMIN_EMAILS", ADMIN_EMAIL):
        yield


@pytest_asyncio.fixture
async def admin_logged_in(api_client: AsyncClient, admin_env) -> AsyncClient:
    r = await api_client.post(
        "/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert r.status_code == 200, r.text
    return api_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _full_wizard_payload(
    *, slug: str = "wiz-org", subdomain: str | None = "wizorg",
    ceo_email: str = "ceo@example.com", send_invite: bool = True,
) -> dict:
    return {
        "step1": {
            "name": "Wizard Org",
            "legal_name": "Wizard Org Ltd",
            "tax_id": "5800012345",
            "address": "1 Wizard St",
            "slug": slug,
        },
        "step2": {
            "logo_url": None,
            "primary_color": "#FF6B35",
            "subdomain": subdomain,
        },
        "step3": {
            "structure_type": "regions_branches",
            "monthly_fee_cents": 500000,
            "setup_fee_cents": 1000000,
            "trial_days": 60,
            "contract_start": "2026-01-01",
            "status": "trial",
        },
        "step4": {
            "full_name": "CEO Person",
            "email": ceo_email,
            "phone": "050-1234567",
            "role": "org_admin",
            "send_invite_immediately": send_invite,
        },
    }


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


async def test_preflight_returns_available_when_free(admin_logged_in: AsyncClient):
    r = await admin_logged_in.post(
        "/admin/api/orgs/wizard/preflight",
        json={"slug": "fresh-slug", "subdomain": "fresh"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["slug_available"] is True
    assert body["subdomain_available"] is True


async def test_preflight_invalid_slug_returns_422(admin_logged_in: AsyncClient):
    r = await admin_logged_in.post(
        "/admin/api/orgs/wizard/preflight",
        json={"slug": "Bad Slug!", "subdomain": None},
    )
    assert r.status_code == 422
    assert r.json().get("code") == "invalid_slug"


async def test_preflight_detects_taken_slug(
    admin_logged_in: AsyncClient, api_session_factory,
):
    from src.models.organizations import Organization
    async with api_session_factory() as s:
        s.add(Organization(slug="already-taken", name="Pre-existing"))
        await s.commit()

    r = await admin_logged_in.post(
        "/admin/api/orgs/wizard/preflight",
        json={"slug": "already-taken", "subdomain": None},
    )
    assert r.status_code == 200
    assert r.json()["slug_available"] is False


# ---------------------------------------------------------------------------
# Commit — happy path
# ---------------------------------------------------------------------------


async def test_commit_happy_path_creates_org_membership_and_audit(
    admin_logged_in: AsyncClient, api_session_factory,
):
    from src.models.org_audit import OrgAuditLog
    from src.models.organizations import Organization
    from src.models.user_organizations import UserOrganization
    from src.models.users import User

    r = await admin_logged_in.post(
        "/admin/api/orgs/wizard/commit",
        json=_full_wizard_payload(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert isinstance(body["org_id"], int)
    assert body["slug"] == "wiz-org"
    assert body["ceo_invite_email_sent"] is True

    async with api_session_factory() as s:
        # Org row has every wizard field populated.
        org = (await s.execute(
            select(Organization).where(Organization.id == body["org_id"])
        )).scalar_one()
        assert org.name == "Wizard Org"
        assert org.legal_name == "Wizard Org Ltd"
        assert org.tax_id == "5800012345"
        assert org.subdomain == "wizorg"
        assert org.primary_color == "#FF6B35"
        assert org.structure_type == "regions_branches"
        assert org.monthly_fee_cents == 500000
        assert org.setup_fee_cents == 1000000
        assert org.trial_ends_at is not None  # status=trial → trial_ends_at set
        assert (org.attributes_json or {}).get("billing_status") == "trial"

        # CEO user + membership.
        ceo = (await s.execute(
            select(User).where(User.email == "ceo@example.com")
        )).scalar_one()
        membership = (await s.execute(
            select(UserOrganization).where(
                UserOrganization.user_id == ceo.id,
                UserOrganization.organization_id == org.id,
            )
        )).scalar_one()
        assert membership.role == "org_admin"

        # Audit row.
        audit = (await s.execute(
            select(OrgAuditLog).where(
                OrgAuditLog.organization_id == org.id,
                OrgAuditLog.action == "org.create.wizard",
            )
        )).scalar_one()
        assert audit.actor_email == ADMIN_EMAIL
        assert audit.actor_user_id is None  # System Admin has no user row


# ---------------------------------------------------------------------------
# Commit — conflicts
# ---------------------------------------------------------------------------


async def test_commit_duplicate_slug_returns_409(
    admin_logged_in: AsyncClient, api_session_factory,
):
    from src.models.organizations import Organization
    async with api_session_factory() as s:
        s.add(Organization(slug="dupe", name="Existing"))
        await s.commit()

    r = await admin_logged_in.post(
        "/admin/api/orgs/wizard/commit",
        json=_full_wizard_payload(slug="dupe"),
    )
    assert r.status_code == 409
    assert r.json()["code"] == "slug_taken"


async def test_commit_duplicate_subdomain_returns_409(
    admin_logged_in: AsyncClient, api_session_factory,
):
    from src.models.organizations import Organization
    async with api_session_factory() as s:
        s.add(Organization(slug="other", name="Existing", subdomain="ours"))
        await s.commit()

    r = await admin_logged_in.post(
        "/admin/api/orgs/wizard/commit",
        json=_full_wizard_payload(slug="brand-new", subdomain="ours"),
    )
    assert r.status_code == 409
    assert r.json()["code"] == "subdomain_taken"


async def test_commit_active_status_skips_trial_end(
    admin_logged_in: AsyncClient, api_session_factory,
):
    from src.models.organizations import Organization

    payload = _full_wizard_payload(slug="paid-now", subdomain="paid")
    payload["step3"]["status"] = "active"
    payload["step3"]["trial_days"] = 0
    r = await admin_logged_in.post("/admin/api/orgs/wizard/commit", json=payload)
    assert r.status_code == 201

    async with api_session_factory() as s:
        org = (await s.execute(
            select(Organization).where(Organization.slug == "paid-now")
        )).scalar_one()
        assert org.trial_ends_at is None


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


async def test_anonymous_cannot_preflight(api_client: AsyncClient):
    r = await api_client.post(
        "/admin/api/orgs/wizard/preflight",
        json={"slug": "x", "subdomain": None},
    )
    # 401 (no admin session) or 422 (rejected validation) both prove the
    # endpoint blocked the call before doing anything. The actual current
    # behavior of admin auth dep is 401.
    assert r.status_code in (401, 403)


async def test_anonymous_cannot_commit(api_client: AsyncClient):
    r = await api_client.post(
        "/admin/api/orgs/wizard/commit", json=_full_wizard_payload(),
    )
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# CEO invite flag
# ---------------------------------------------------------------------------


async def test_commit_with_invite_disabled_does_not_create_invite(
    admin_logged_in: AsyncClient, api_session_factory,
):
    from src.models.org_invites import OrgInvite

    r = await admin_logged_in.post(
        "/admin/api/orgs/wizard/commit",
        json=_full_wizard_payload(
            slug="no-invite", subdomain=None,
            ceo_email="noinvite@example.com", send_invite=False,
        ),
    )
    assert r.status_code == 201
    assert r.json()["ceo_invite_email_sent"] is False

    async with api_session_factory() as s:
        invites = (await s.execute(
            select(OrgInvite).where(OrgInvite.email == "noinvite@example.com")
        )).scalars().all()
        assert invites == []
