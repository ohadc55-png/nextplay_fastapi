"""Tests for /admin/api/orgs/* — System Admin organization management.

All endpoints require an authenticated System Admin session. We replicate
the existing admin-test pattern: patch ADMIN_PASSWORD_HASH + ADMIN_EMAILS
for the duration of the test, log in via /admin/login, then exercise the
/admin/api/orgs/* endpoints.
"""

from __future__ import annotations

from unittest.mock import patch

import bcrypt
import pytest
import pytest_asyncio
from httpx import AsyncClient

from src.core.config import settings

pytestmark = pytest.mark.asyncio

ADMIN_EMAIL = "admin@test.com"
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
        "/admin/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert r.status_code == 200, r.text
    return api_client


class TestAdminOrgsAuth:
    async def test_anonymous_cannot_list(self, api_client: AsyncClient):
        # No admin session, no JWT — must reject. (401 is fine here; the
        # 404-not-403 rule applies to /org/*, not /admin/*.)
        r = await api_client.get("/admin/api/orgs")
        assert r.status_code == 401


class TestAdminOrgsCreate:
    async def test_create_returns_201_and_stores_row(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.post(
            "/admin/api/orgs",
            json={"slug": "shaar-shivyon", "name": "Sha'ar Shivyon"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["slug"] == "shaar-shivyon"
        assert body["name"] == "Sha'ar Shivyon"
        assert isinstance(body["id"], int)

    async def test_invalid_slug_returns_422(self, admin_logged_in: AsyncClient):
        # ValidationError → 422 by global handler
        r = await admin_logged_in.post(
            "/admin/api/orgs",
            json={"slug": "Not Valid Slug!", "name": "Bad"},
        )
        assert r.status_code == 422
        assert r.json()["code"] == "invalid_slug"

    async def test_duplicate_slug_returns_409(self, admin_logged_in: AsyncClient):
        await admin_logged_in.post(
            "/admin/api/orgs", json={"slug": "dup-slug", "name": "First"},
        )
        r = await admin_logged_in.post(
            "/admin/api/orgs", json={"slug": "dup-slug", "name": "Second"},
        )
        assert r.status_code == 409


class TestAdminOrgsList:
    async def test_list_excludes_archived_by_default(
        self, admin_logged_in: AsyncClient
    ):
        c1 = await admin_logged_in.post(
            "/admin/api/orgs", json={"slug": "alpha", "name": "Alpha"},
        )
        c2 = await admin_logged_in.post(
            "/admin/api/orgs", json={"slug": "beta", "name": "Beta"},
        )
        await admin_logged_in.delete(f"/admin/api/orgs/{c1.json()['id']}")

        r = await admin_logged_in.get("/admin/api/orgs")
        assert r.status_code == 200
        slugs = [o["slug"] for o in r.json()["organizations"]]
        assert "beta" in slugs
        assert "alpha" not in slugs  # soft-deleted excluded by default

        # With include_archived=true the archived row reappears.
        r = await admin_logged_in.get("/admin/api/orgs?include_archived=true")
        slugs_all = [o["slug"] for o in r.json()["organizations"]]
        assert "alpha" in slugs_all


class TestAdminOrgsUpdate:
    async def test_patch_changes_name_and_logs_audit(
        self, admin_logged_in: AsyncClient
    ):
        created = await admin_logged_in.post(
            "/admin/api/orgs", json={"slug": "rename-me", "name": "Old Name"},
        )
        org_id = created.json()["id"]

        r = await admin_logged_in.patch(
            f"/admin/api/orgs/{org_id}", json={"name": "New Name"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "New Name"

        audit = await admin_logged_in.get(f"/admin/api/orgs/{org_id}/audit")
        actions = [r["action"] for r in audit.json()["audit_log"]]
        assert "org.update" in actions
        assert "org.create" in actions  # the creation also logged


class TestAdminOrgsAdminAssign:
    async def test_assign_admin_creates_membership(
        self, admin_logged_in: AsyncClient, register_user
    ):
        u = await register_user("ceo@example.com")
        org = (await admin_logged_in.post(
            "/admin/api/orgs",
            json={"slug": "assign-test", "name": "Assign Test"},
        )).json()
        r = await admin_logged_in.post(
            f"/admin/api/orgs/{org['id']}/admins",
            json={"user_id": u["id"]},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["user_id"] == u["id"]
        assert body["role"] == "org_admin"

    async def test_assign_admin_is_idempotent(
        self, admin_logged_in: AsyncClient, register_user
    ):
        u = await register_user("twice@example.com")
        org = (await admin_logged_in.post(
            "/admin/api/orgs",
            json={"slug": "idempotent-test", "name": "Idempotent"},
        )).json()
        r1 = await admin_logged_in.post(
            f"/admin/api/orgs/{org['id']}/admins",
            json={"user_id": u["id"]},
        )
        r2 = await admin_logged_in.post(
            f"/admin/api/orgs/{org['id']}/admins",
            json={"user_id": u["id"]},
        )
        # Second call returns the existing membership row, NOT a duplicate.
        assert r1.json()["id"] == r2.json()["id"]

    async def test_assign_to_nonexistent_user_returns_404(
        self, admin_logged_in: AsyncClient
    ):
        org = (await admin_logged_in.post(
            "/admin/api/orgs",
            json={"slug": "no-user", "name": "NoUser"},
        )).json()
        r = await admin_logged_in.post(
            f"/admin/api/orgs/{org['id']}/admins",
            json={"user_id": 999_999},
        )
        assert r.status_code == 404


class TestAdminOrgsAuditTrail:
    async def test_audit_log_records_create(self, admin_logged_in: AsyncClient):
        org = (await admin_logged_in.post(
            "/admin/api/orgs", json={"slug": "audited", "name": "Audited"},
        )).json()
        r = await admin_logged_in.get(f"/admin/api/orgs/{org['id']}/audit")
        body = r.json()
        assert len(body["audit_log"]) >= 1
        first = body["audit_log"][0]
        assert first["action"] == "org.create"
        assert first["actor_email"] == ADMIN_EMAIL
        assert first["actor_user_id"] is None  # System Admin has no user row
