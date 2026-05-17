"""Tests for the short-code self-service redemption flow.

Coverage:
- Invite creation returns a `short_code` field
- The code is 8 chars from the curated alphabet
- POST /org/api/invites/redeem with the code creates a NEW user (any email)
  + membership, and stamps the org session keys for auto-login
- Same code used twice → second call 404 (one-time-use across BOTH paths)
- The companion magic-link token is invalidated by code redemption
- Invalid / unknown / expired code → 404
- Email already in use → 400 with `email_in_use` code
- Password too short → 422
- Code is normalized (dashes / spaces / lowercase tolerated)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


async def _issue_invite(
    client: AsyncClient, *, email: str = "newcoach@org.test", role: str = "coach"
) -> dict:
    r = await client.post(
        "/org/api/users/invite",
        json={"email": email, "role": role},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Invite issue exposes short_code
# ---------------------------------------------------------------------------


async def test_invite_response_includes_short_code(org_admin_client: AsyncClient):
    data = await _issue_invite(org_admin_client)
    code = data.get("short_code")
    assert code is not None and len(code) == 8
    # Must be from the curated alphabet (no 0/O/1/I/L)
    assert all(ch in "ABCDEFGHJKMNPQRSTUVWXYZ23456789" for ch in code), code


async def test_invite_codes_are_unique_across_invites(
    org_admin_client: AsyncClient,
):
    a = await _issue_invite(org_admin_client, email="a@org.test")
    b = await _issue_invite(org_admin_client, email="b@org.test")
    assert a["short_code"] != b["short_code"]


# ---------------------------------------------------------------------------
# Redemption — happy path
# ---------------------------------------------------------------------------


async def test_redeem_creates_user_and_auto_logs_in(
    org_admin_client: AsyncClient, api_client: AsyncClient,
):
    invite = await _issue_invite(org_admin_client, email="placeholder@org.test")
    code = invite["short_code"]

    # The invitee uses a DIFFERENT email than what the inviter typed.
    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "realuser@org.test",
            "full_name": "Real User",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # Phase 14 — coach role redirects to the Coach App home (`/`), not the
    # org dashboard. The org session is still stamped so /org/api/* works.
    assert body["redirect"] == "/"

    # Session keys are set → can hit a logged-in endpoint without re-login.
    r2 = await api_client.get("/org/api/dashboard/summary")
    assert r2.status_code == 200, r2.text
    summary = r2.json()
    assert summary["role"] == invite["role"]


async def test_redeem_normalizes_code_with_dashes_and_spaces(
    org_admin_client: AsyncClient, api_client: AsyncClient,
):
    invite = await _issue_invite(org_admin_client, email="placeholder2@org.test")
    raw = invite["short_code"]
    pretty = raw[:4] + "-" + raw[4:]

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": pretty.lower(),  # mixed case + dash
            "email": "norm@org.test",
            "full_name": "Norm User",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# One-time-use
# ---------------------------------------------------------------------------


async def test_redeem_twice_with_same_code_fails(
    org_admin_client: AsyncClient, api_client: AsyncClient,
):
    invite = await _issue_invite(org_admin_client, email="once@org.test")
    code = invite["short_code"]

    first = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "first@org.test",
            "full_name": "First",
            "password": "Sup3rSecure!",
        },
    )
    assert first.status_code == 200

    # Fresh client for the second attempt — no carried-over session.
    second_client = api_client
    # Clear cookies so we attempt as a "new" person
    second_client.cookies.clear()
    second = await second_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "second@org.test",
            "full_name": "Second",
            "password": "Sup3rSecure!",
        },
    )
    assert second.status_code == 404


async def test_redemption_kills_companion_magic_link_token(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """After short-code redemption the auth_token's used_at must be stamped,
    so the magic-link endpoint can't redeem the same invite again."""
    from src.models.auth import AuthToken
    from src.models.org_invites import OrgInvite

    invite = await _issue_invite(org_admin_client, email="link@org.test")
    code = invite["short_code"]

    # Redeem via short code
    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "viaCode@org.test",
            "full_name": "Via Code",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200

    # Now check that the auth_token tied to the invite is marked used.
    async with api_session_factory() as s:
        inv = (
            await s.execute(
                select(OrgInvite).where(OrgInvite.short_code == code)
            )
        ).scalar_one()
        tok = await s.get(AuthToken, inv.auth_token_id)
        assert tok is not None
        assert tok.used_at is not None
        assert inv.status == "accepted"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


async def test_redeem_with_unknown_code_returns_404(api_client: AsyncClient):
    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": "ZZZZZZZZ",
            "email": "x@org.test",
            "full_name": "Unknown",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 404


async def test_redeem_with_existing_email_wrong_password_is_cloaked(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """Phase 14 — when the email entered on /org/join already has an account,
    the redeemer must prove ownership by providing the right password. Wrong
    password → 404 cloak (never confirm whether the email is registered).
    Phase 14 supersedes the old "email_in_use" rejection: a trial user CAN
    legitimately accept an org invite and have their account converted to
    club — but only after password verification.
    """
    from src.auth.password_service import hash_password
    from src.models.users import User

    invite = await _issue_invite(org_admin_client, email="placeholder3@org.test")
    code = invite["short_code"]

    # Seed an existing user — different password than the redeemer attempts.
    async with api_session_factory() as s:
        s.add(User(
            email="taken@org.test",
            password_hash=hash_password("RealPassword!"),
            display_name="Existing",
            email_verified=True,
        ))
        await s.commit()

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "taken@org.test",
            "full_name": "Conflict",
            "password": "WrongGuess!",  # not the real password
        },
    )
    assert r.status_code == 404


async def test_redeem_with_expired_token_returns_404(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    from src.models.auth import AuthToken
    from src.models.org_invites import OrgInvite

    invite = await _issue_invite(org_admin_client, email="exp@org.test")
    code = invite["short_code"]

    # Backdate the auth_token expiry so it's already past.
    async with api_session_factory() as s:
        inv = (
            await s.execute(
                select(OrgInvite).where(OrgInvite.short_code == code)
            )
        ).scalar_one()
        tok = await s.get(AuthToken, inv.auth_token_id)
        tok.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        await s.commit()

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "expired@org.test",
            "full_name": "Expired",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 404


async def test_redeem_with_short_password_rejected_by_validator(
    org_admin_client: AsyncClient, api_client: AsyncClient,
):
    invite = await _issue_invite(org_admin_client, email="weak@org.test")
    code = invite["short_code"]
    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "weak@org.test",
            "full_name": "Weak",
            "password": "short",  # under 8 chars
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Redemption with scoped invite (program/region) → membership carries scope
# ---------------------------------------------------------------------------


async def test_redeem_program_manager_invite_propagates_program_id(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    from src.models.programs import Program
    from src.models.user_organizations import UserOrganization

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        prog = Program(organization_id=org_id, name="בועטות")
        s.add(prog)
        await s.commit()
        prog_id = prog.id

    invite = await org_admin_client.post(
        "/org/api/users/invite",
        json={
            "email": "pm-invitee@org.test",
            "role": "program_manager",
            "program_id": prog_id,
        },
    )
    assert invite.status_code == 201, invite.text
    code = invite.json()["short_code"]

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "real-pm@org.test",
            "full_name": "Real PM",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200, r.text

    async with api_session_factory() as s:
        from src.models.users import User
        user = (await s.execute(
            select(User).where(User.email == "real-pm@org.test")
        )).scalar_one()
        membership = (await s.execute(
            select(UserOrganization).where(UserOrganization.user_id == user.id)
        )).scalar_one()
        assert membership.role == "program_manager"
        assert membership.program_id == prog_id
