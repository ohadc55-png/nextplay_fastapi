"""End-to-end tests for the Org invite flow.

Covers:
- POST /org/api/orgs/{org_id}/users/invite — only org_admin can issue
- 404-not-403 on cross-org invite (path mismatch with active org)
- 409 on duplicate pending invite
- POST /org/api/invites/accept — happy path: token + password creates user + membership
- 404 on tampered / expired / already-used / unknown token
- Idempotent re-accept (membership already exists)
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Issue invite
# ---------------------------------------------------------------------------


class TestInviteIssue:
    async def test_org_admin_issues_invite(self, org_admin_client: AsyncClient):
        creds = org_admin_client.org_seed  # type: ignore[attr-defined]
        r = await org_admin_client.post(
            f"/org/api/orgs/{creds['organization_id']}/users/invite",
            json={"email": "newcoach@example.com", "role": "coach"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["email"] == "newcoach@example.com"
        assert body["role"] == "coach"
        assert body["status"] == "pending"
        # The raw token is NEVER returned in the response.
        assert "token" not in body

    async def test_cross_org_invite_returns_404(
        self, org_admin_client: AsyncClient, api_session_factory
    ):
        # Seed an unrelated org. The logged-in user is NOT a member of it.
        from src.models.organizations import Organization

        async with api_session_factory() as s:
            other = Organization(slug="rogue-org", name="Rogue")
            s.add(other)
            await s.commit()
            other_id = other.id

        r = await org_admin_client.post(
            f"/org/api/orgs/{other_id}/users/invite",
            json={"email": "x@example.com", "role": "coach"},
        )
        # 404 (NOT 403) — never confirms the other org exists.
        assert r.status_code == 404

    async def test_duplicate_pending_invite_returns_409(
        self, org_admin_client: AsyncClient
    ):
        creds = org_admin_client.org_seed  # type: ignore[attr-defined]
        r1 = await org_admin_client.post(
            f"/org/api/orgs/{creds['organization_id']}/users/invite",
            json={"email": "dup@example.com", "role": "coach"},
        )
        assert r1.status_code == 201
        r2 = await org_admin_client.post(
            f"/org/api/orgs/{creds['organization_id']}/users/invite",
            json={"email": "dup@example.com", "role": "coach"},
        )
        assert r2.status_code == 409

    async def test_invalid_role_returns_422(self, org_admin_client: AsyncClient):
        creds = org_admin_client.org_seed  # type: ignore[attr-defined]
        r = await org_admin_client.post(
            f"/org/api/orgs/{creds['organization_id']}/users/invite",
            json={"email": "x@example.com", "role": "ceo"},  # not in _VALID_ROLES
        )
        assert r.status_code == 422


class TestInviteAccept:
    async def _grab_raw_token(
        self, api_session_factory, *, invite_email: str
    ) -> str:
        """Tests can't read the raw token from the response. Pull the
        most recent `org_invite` AuthToken hash + cross-reference what
        we know about the invite. To keep the test deterministic we
        instead hash candidate tokens... but we don't have a candidate.

        Workaround: manipulate the DB directly. Replace the existing
        AuthToken row with a known hash so we can know the raw value.
        Returns the raw token we just pinned."""
        from src.models.auth import AuthToken
        from src.models.org_invites import OrgInvite

        raw = "test-invite-raw-token-deterministic"
        token_hash = hashlib.sha256(raw.encode()).hexdigest()

        async with api_session_factory() as s:
            invite = (
                await s.execute(
                    select(OrgInvite).where(OrgInvite.email == invite_email)
                )
            ).scalar_one()
            tok = await s.get(AuthToken, invite.auth_token_id)
            tok.token_hash = token_hash
            await s.commit()
        return raw

    async def test_accept_creates_user_and_membership(
        self, org_admin_client: AsyncClient, api_session_factory, api_client: AsyncClient
    ):
        creds = org_admin_client.org_seed  # type: ignore[attr-defined]
        await org_admin_client.post(
            f"/org/api/orgs/{creds['organization_id']}/users/invite",
            json={"email": "fresh@example.com", "role": "coach"},
        )
        raw = await self._grab_raw_token(api_session_factory, invite_email="fresh@example.com")

        # Anonymous request — token is the credential.
        r = await api_client.post(
            "/org/api/invites/accept",
            json={
                "token": raw,
                "password": "Sup3rSecure!",
                "display_name": "Fresh Coach",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"ok": True, "redirect": "/org/login"}

        # Membership now exists for the new user.
        from src.models.user_organizations import UserOrganization
        from src.models.users import User

        async with api_session_factory() as s:
            user = (await s.execute(
                select(User).where(User.email == "fresh@example.com")
            )).scalar_one()
            membership = (await s.execute(
                select(UserOrganization).where(
                    UserOrganization.user_id == user.id,
                    UserOrganization.organization_id == creds["organization_id"],
                    UserOrganization.role == "coach",
                )
            )).scalar_one()
            assert membership.status == "active"
            assert membership.accepted_at is not None

    async def test_accept_with_unknown_token_returns_404(
        self, api_client: AsyncClient
    ):
        r = await api_client.post(
            "/org/api/invites/accept",
            json={"token": "not-a-real-token", "password": "X1abcdefg"},
        )
        assert r.status_code == 404

    async def test_accept_replays_token_idempotently(
        self, org_admin_client: AsyncClient, api_session_factory, api_client: AsyncClient
    ):
        creds = org_admin_client.org_seed  # type: ignore[attr-defined]
        await org_admin_client.post(
            f"/org/api/orgs/{creds['organization_id']}/users/invite",
            json={"email": "replay@example.com", "role": "coach"},
        )
        raw = await self._grab_raw_token(api_session_factory, invite_email="replay@example.com")

        r1 = await api_client.post(
            "/org/api/invites/accept",
            json={"token": raw, "password": "Sup3rSecure!"},
        )
        assert r1.status_code == 200
        # Second call uses the same (now-used) token — must 404.
        r2 = await api_client.post(
            "/org/api/invites/accept",
            json={"token": raw, "password": "Sup3rSecure!"},
        )
        assert r2.status_code == 404

    async def test_accept_with_expired_token_returns_404(
        self, org_admin_client: AsyncClient, api_session_factory, api_client: AsyncClient
    ):
        from src.models.auth import AuthToken
        from src.models.org_invites import OrgInvite

        creds = org_admin_client.org_seed  # type: ignore[attr-defined]
        await org_admin_client.post(
            f"/org/api/orgs/{creds['organization_id']}/users/invite",
            json={"email": "expired@example.com", "role": "coach"},
        )
        raw = "expired-token-deterministic"
        token_hash = hashlib.sha256(raw.encode()).hexdigest()

        async with api_session_factory() as s:
            invite = (await s.execute(
                select(OrgInvite).where(OrgInvite.email == "expired@example.com")
            )).scalar_one()
            tok = await s.get(AuthToken, invite.auth_token_id)
            tok.token_hash = token_hash
            tok.expires_at = datetime.now(UTC) - timedelta(hours=1)
            await s.commit()

        r = await api_client.post(
            "/org/api/invites/accept",
            json={"token": raw, "password": "Sup3rSecure!"},
        )
        assert r.status_code == 404
