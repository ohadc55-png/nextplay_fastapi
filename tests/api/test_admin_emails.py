"""Admin email management — log + mailing lists + composer stubs.

Composer endpoints (preview / test-send / send) verify the wire format
and return `stub: true` until Phase 7 wires real Resend delivery.
"""

from __future__ import annotations

from unittest.mock import patch

import bcrypt
import pytest_asyncio
from httpx import AsyncClient

from src.core.config import settings

ADMIN_EMAIL = "admin@test.com"
ADMIN_PASSWORD = "AdminPass1"


@pytest_asyncio.fixture
async def admin_logged_in(api_client: AsyncClient):
    pw_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    with patch.object(settings, "ADMIN_PASSWORD_HASH", pw_hash), \
         patch.object(settings, "ADMIN_EMAILS", ADMIN_EMAIL), \
         patch.object(settings, "ADMIN_EMAIL", ADMIN_EMAIL):
        r = await api_client.post(
            "/admin/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        assert r.status_code == 200, r.text
        yield api_client


# ---------------------------------------------------------------------------
# Log viewer (empty DB returns clean zeros)
# ---------------------------------------------------------------------------

class TestEmailLog:
    async def test_empty_log_returns_zero_counts(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.get("/admin/api/emails")
        assert r.status_code == 200
        body = r.json()
        assert body["total_24h"] == 0
        assert body["total_7d"] == 0
        assert body["total_30d"] == 0
        assert body["failed_30d"] == 0
        assert body["rows"] == []

    async def test_templates_endpoint_returns_distinct_list(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.get("/admin/api/emails/templates")
        assert r.status_code == 200
        assert "templates" in r.json()


# ---------------------------------------------------------------------------
# Mailing lists CRUD
# ---------------------------------------------------------------------------

class TestMailingLists:
    async def test_full_list_lifecycle(
        self, admin_logged_in: AsyncClient, register_user
    ):
        # Create list
        r = await admin_logged_in.post(
            "/admin/api/emails/lists",
            json={"name": "Pro Coaches", "description": "Active Pro plan"},
        )
        assert r.status_code == 200
        list_id = r.json()["id"]

        # Duplicate name → 400
        r = await admin_logged_in.post(
            "/admin/api/emails/lists", json={"name": "Pro Coaches"}
        )
        assert r.status_code == 400

        # GET lists shows it with member_count=0
        r = await admin_logged_in.get("/admin/api/emails/lists")
        lists = r.json()["lists"]
        assert any(l["id"] == list_id and l["member_count"] == 0 for l in lists)

        # Add a member (after seeding a user)
        await register_user("coach@example.com")
        r = await admin_logged_in.post(
            f"/admin/api/emails/lists/{list_id}/members",
            json={"email": "coach@example.com"},
        )
        assert r.status_code == 200

        # Members endpoint shows the user
        r = await admin_logged_in.get(
            f"/admin/api/emails/lists/{list_id}/members"
        )
        assert len(r.json()["members"]) == 1
        member = r.json()["members"][0]
        assert member["email"] == "coach@example.com"

        # Adding the same member again is idempotent
        r = await admin_logged_in.post(
            f"/admin/api/emails/lists/{list_id}/members",
            json={"email": "coach@example.com"},
        )
        assert r.status_code == 200

        # Add unknown email → 404
        r = await admin_logged_in.post(
            f"/admin/api/emails/lists/{list_id}/members",
            json={"email": "ghost@example.com"},
        )
        assert r.status_code == 404

        # Remove the member
        r = await admin_logged_in.delete(
            f"/admin/api/emails/lists/{list_id}/members/{member['id']}"
        )
        assert r.status_code == 200
        r = await admin_logged_in.get(
            f"/admin/api/emails/lists/{list_id}/members"
        )
        assert r.json()["members"] == []

        # Delete the list
        r = await admin_logged_in.delete(f"/admin/api/emails/lists/{list_id}")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Composer (Phase 7 will wire real send; tests verify wire shape + stubs)
# ---------------------------------------------------------------------------

class TestComposer:
    async def test_preview_renders_simple_html(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.post(
            "/admin/api/emails/preview",
            json={"subject": "Hello", "body": "Big update.\n\nLine 2."},
        )
        assert r.status_code == 200
        body = r.json()
        assert "Hello" in body["html"]
        assert "<p" in body["html"]
        assert body["stub"] is True

    async def test_preview_requires_body(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.post(
            "/admin/api/emails/preview", json={"subject": "x", "body": ""}
        )
        assert r.status_code == 400

    async def test_test_send_returns_stub_to_admin(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.post(
            "/admin/api/emails/test-send",
            json={"subject": "S", "body": "B"},
        )
        assert r.status_code == 200
        assert r.json()["to"] == ADMIN_EMAIL
        assert r.json()["stub"] is True

    async def test_broadcast_all_resolves_marketing_optedin_users(
        self, admin_logged_in: AsyncClient, register_user
    ):
        await register_user("marketing-ok@example.com")
        r = await admin_logged_in.post(
            "/admin/api/emails/send",
            json={"subject": "S", "body": "B", "mode": "all"},
        )
        assert r.status_code == 200
        # Default email_marketing=True so the new user counts.
        assert r.json()["recipient_count"] >= 1
        assert r.json()["stub"] is True

    async def test_broadcast_specific_requires_user_ids(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.post(
            "/admin/api/emails/send",
            json={"subject": "S", "body": "B", "mode": "specific"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# User search (composer 'specific users' mode)
# ---------------------------------------------------------------------------

class TestUserSearch:
    async def test_empty_q_returns_empty_list(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.get("/admin/api/emails/users?q=")
        assert r.status_code == 200
        assert r.json() == {"users": []}

    async def test_substring_match(
        self, admin_logged_in: AsyncClient, register_user
    ):
        await register_user("findme@example.com")
        r = await admin_logged_in.get("/admin/api/emails/users?q=findme")
        assert r.status_code == 200
        assert any(u["email"] == "findme@example.com" for u in r.json()["users"])
