"""Tests for /org/api/users/* (Phase 1.4)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures specific to user management
# ---------------------------------------------------------------------------


async def _seed_region(api_session_factory, org_id: int, name: str = "Region 1") -> int:
    from src.models.regions import Region
    async with api_session_factory() as s:
        r = Region(organization_id=org_id, name=name)
        s.add(r)
        await s.commit()
        return r.id


async def _seed_member(
    api_session_factory,
    org_id: int,
    *,
    email: str,
    role: str = "coach",
    region_id: int | None = None,
    branch_id: int | None = None,
    program_id: int | None = None,
) -> dict:
    """Create a User + UserOrganization pair. Returns (user_id, membership_id)."""
    from src.auth.password_service import hash_password
    from src.models.user_organizations import UserOrganization
    from src.models.users import User
    async with api_session_factory() as s:
        u = User(
            email=email,
            password_hash=hash_password("Sup3rSecure!"),
            display_name=email.split("@")[0],
            email_verified=True,
        )
        s.add(u)
        await s.flush()
        uo = UserOrganization(
            user_id=u.id,
            organization_id=org_id,
            role=role,
            region_id=region_id,
            branch_id=branch_id,
            program_id=program_id,
            status="active",
        )
        s.add(uo)
        await s.commit()
        return {"user_id": u.id, "membership_id": uo.id}


# ---------------------------------------------------------------------------
# GET /org/api/users — list members
# ---------------------------------------------------------------------------


async def test_list_members_returns_self_for_solo_admin(
    org_admin_client: AsyncClient,
):
    r = await org_admin_client.get("/org/api/users")
    assert r.status_code == 200
    members = r.json()["members"]
    assert len(members) == 1
    me = members[0]
    assert me["email"] == org_admin_client.org_seed["email"]
    assert me["role"] == "org_admin"
    assert me["status"] == "active"


async def test_list_members_includes_invited_users(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    await _seed_member(api_session_factory, org_id, email="coach1@org.test")
    r = await org_admin_client.get("/org/api/users")
    members = r.json()["members"]
    assert len(members) == 2
    emails = {m["email"] for m in members}
    assert "coach1@org.test" in emails


async def test_region_manager_sees_only_region_members(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    creds = await seed_org_admin(email="rm@org.test", role="region_manager")
    org_id = creds["organization_id"]
    region_a = await _seed_region(api_session_factory, org_id, name="A")
    region_b = await _seed_region(api_session_factory, org_id, name="B")

    # Scope the region_manager to region A.
    async with api_session_factory() as s:
        from src.models.user_organizations import UserOrganization
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "region_manager",
            )
            .values(region_id=region_a)
        )
        await s.commit()

    # Add one coach in region A, one in region B.
    await _seed_member(api_session_factory, org_id, email="ca@org.test", region_id=region_a)
    await _seed_member(api_session_factory, org_id, email="cb@org.test", region_id=region_b)

    r = await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    assert r.status_code == 200

    r = await api_client.get("/org/api/users")
    members = r.json()["members"]
    emails = {m["email"] for m in members}
    assert "ca@org.test" in emails
    assert "cb@org.test" not in emails


# ---------------------------------------------------------------------------
# POST /org/api/users/invite — create invite
# ---------------------------------------------------------------------------


async def test_invite_member_happy_path(org_admin_client: AsyncClient):
    r = await org_admin_client.post(
        "/org/api/users/invite",
        json={"email": "new@org.test", "role": "coach"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "new@org.test"
    assert body["role"] == "coach"
    assert body["status"] == "pending"


async def test_invite_duplicate_pending_returns_409(org_admin_client: AsyncClient):
    await org_admin_client.post(
        "/org/api/users/invite", json={"email": "dup@org.test", "role": "coach"}
    )
    r = await org_admin_client.post(
        "/org/api/users/invite", json={"email": "dup@org.test", "role": "coach"}
    )
    assert r.status_code == 409
    assert r.json()["code"] == "invite_pending"


async def test_region_manager_cannot_invite_org_admin(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """Role-escalation guard: region_manager → org_admin attempt = 404."""
    creds = await seed_org_admin(email="rm-esc@org.test", role="region_manager")
    region_id = await _seed_region(api_session_factory, creds["organization_id"])
    async with api_session_factory() as s:
        from src.models.user_organizations import UserOrganization
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "region_manager",
            )
            .values(region_id=region_id)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.post(
        "/org/api/users/invite",
        json={"email": "promoted@org.test", "role": "org_admin"},
    )
    assert r.status_code == 404


async def test_region_manager_invite_forced_to_own_region(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """region_manager that omits region_id gets it auto-filled to own region.
    Attempting another region's id → 404."""
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="rm-scope@org.test", role="region_manager")
    org_id = creds["organization_id"]
    region_a = await _seed_region(api_session_factory, org_id, name="A")
    region_b = await _seed_region(api_session_factory, org_id, name="B")
    async with api_session_factory() as s:
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "region_manager",
            )
            .values(region_id=region_a)
        )
        await s.commit()

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )

    # No region_id → forced to A.
    r1 = await api_client.post(
        "/org/api/users/invite",
        json={"email": "c1@org.test", "role": "coach"},
    )
    assert r1.status_code == 201
    assert r1.json()["region_id"] == region_a

    # Different region_id → 404.
    r2 = await api_client.post(
        "/org/api/users/invite",
        json={"email": "c2@org.test", "role": "coach", "region_id": region_b},
    )
    assert r2.status_code == 404


async def test_invite_with_region_validates_org(
    org_admin_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """Phase 12 — region_id pointing at another org's region → 404 cross-org.
    (Replaces the legacy branch_id version; branch_manager + Branch retired.)
    Includes a same-org program_id so we get past the RM scope validator and
    actually hit the region cross-org check."""
    from src.models.programs import Program
    from src.models.regions import Region

    org_id = org_admin_client.org_seed["organization_id"]
    other = await seed_org_admin(email="other@org.test", org_slug="other-org", org_name="Other")
    async with api_session_factory() as s:
        local_program = Program(organization_id=org_id, name="Local")
        s.add(local_program)
        foreign = Region(organization_id=other["organization_id"], name="Foreign")
        s.add(foreign)
        await s.commit()
        local_program_id = local_program.id
        foreign_id = foreign.id

    r = await org_admin_client.post(
        "/org/api/users/invite",
        json={
            "email": "r@org.test", "role": "region_manager",
            "program_id": local_program_id, "region_id": foreign_id,
        },
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Pending invites + resend + cancel
# ---------------------------------------------------------------------------


async def test_list_pending_invites(org_admin_client: AsyncClient):
    await org_admin_client.post(
        "/org/api/users/invite", json={"email": "p@org.test", "role": "coach"}
    )
    r = await org_admin_client.get("/org/api/users/invites/pending")
    assert r.status_code == 200
    invites = r.json()["invites"]
    assert len(invites) == 1
    assert invites[0]["email"] == "p@org.test"
    assert invites[0]["status"] == "pending"


async def test_resend_invite_invalidates_old_token(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.auth import AuthToken
    from src.models.org_invites import OrgInvite

    create = await org_admin_client.post(
        "/org/api/users/invite", json={"email": "r@org.test", "role": "coach"}
    )
    invite_id = create.json()["id"]

    async with api_session_factory() as s:
        invite = (await s.execute(select(OrgInvite).where(OrgInvite.id == invite_id))).scalar_one()
        old_token_id = invite.auth_token_id

    r = await org_admin_client.post(f"/org/api/users/invites/{invite_id}/resend")
    assert r.status_code == 200

    async with api_session_factory() as s:
        old_token = await s.get(AuthToken, old_token_id)
        assert old_token.used_at is not None  # old token retired

        invite_after = (await s.execute(select(OrgInvite).where(OrgInvite.id == invite_id))).scalar_one()
        assert invite_after.auth_token_id != old_token_id  # new token issued
        assert invite_after.status == "pending"


async def test_cancel_invite(org_admin_client: AsyncClient, api_session_factory):
    from src.models.org_invites import OrgInvite

    create = await org_admin_client.post(
        "/org/api/users/invite", json={"email": "x@org.test", "role": "coach"}
    )
    invite_id = create.json()["id"]
    r = await org_admin_client.delete(f"/org/api/users/invites/{invite_id}")
    assert r.status_code == 200

    async with api_session_factory() as s:
        invite = (await s.execute(select(OrgInvite).where(OrgInvite.id == invite_id))).scalar_one()
        assert invite.status == "cancelled"


# ---------------------------------------------------------------------------
# PATCH /org/api/users/{membership_id}
# ---------------------------------------------------------------------------


async def test_patch_member_role(org_admin_client: AsyncClient, api_session_factory):
    org_id = org_admin_client.org_seed["organization_id"]
    seeded = await _seed_member(api_session_factory, org_id, email="m@org.test", role="coach")
    mid = seeded["membership_id"]

    r = await org_admin_client.patch(
        f"/org/api/users/{mid}", json={"role": "viewer"}
    )
    assert r.status_code == 200
    assert r.json()["role"] == "viewer"


async def test_patch_self_demotion_blocked(org_admin_client: AsyncClient, api_session_factory):
    """Cannot demote your own org_admin row."""
    from src.models.user_organizations import UserOrganization

    org_id = org_admin_client.org_seed["organization_id"]
    user_id = org_admin_client.org_seed["user_id"]
    async with api_session_factory() as s:
        own_mid = (
            await s.execute(
                select(UserOrganization.id).where(
                    UserOrganization.user_id == user_id,
                    UserOrganization.organization_id == org_id,
                )
            )
        ).scalar_one()

    r = await org_admin_client.patch(
        f"/org/api/users/{own_mid}", json={"role": "viewer"}
    )
    assert r.status_code == 409
    assert r.json()["code"] == "self_demotion_blocked"


async def test_patch_region_manager_validates_region(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Promoting to region_manager without region_id → 422."""
    org_id = org_admin_client.org_seed["organization_id"]
    seeded = await _seed_member(api_session_factory, org_id, email="m2@org.test", role="coach")
    r = await org_admin_client.patch(
        f"/org/api/users/{seeded['membership_id']}",
        json={"role": "region_manager"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "region_required"


# ---------------------------------------------------------------------------
# DELETE /org/api/users/{membership_id}
# ---------------------------------------------------------------------------


async def test_remove_member_soft_deletes(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.user_organizations import UserOrganization
    from src.models.users import User

    org_id = org_admin_client.org_seed["organization_id"]
    seeded = await _seed_member(api_session_factory, org_id, email="rm@org.test")
    mid = seeded["membership_id"]
    uid = seeded["user_id"]

    r = await org_admin_client.delete(f"/org/api/users/{mid}")
    assert r.status_code == 200

    async with api_session_factory() as s:
        uo = await s.get(UserOrganization, mid)
        assert uo.status == "removed"
        user = await s.get(User, uid)
        assert user is not None  # user row preserved


async def test_self_removal_blocked(org_admin_client: AsyncClient, api_session_factory):
    from src.models.user_organizations import UserOrganization

    org_id = org_admin_client.org_seed["organization_id"]
    user_id = org_admin_client.org_seed["user_id"]
    async with api_session_factory() as s:
        own_mid = (
            await s.execute(
                select(UserOrganization.id).where(
                    UserOrganization.user_id == user_id,
                    UserOrganization.organization_id == org_id,
                )
            )
        ).scalar_one()

    r = await org_admin_client.delete(f"/org/api/users/{own_mid}")
    assert r.status_code == 409
    assert r.json()["code"] == "self_removal_blocked"


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_patch_cross_tenant_returns_404(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """org_admin of B cannot patch a membership in A → 404."""
    a = await seed_org_admin(email="a@x.test", org_slug="a-x", org_name="A")
    b = await seed_org_admin(email="b@x.test", org_slug="b-x", org_name="B")
    target = await _seed_member(
        api_session_factory, a["organization_id"], email="victim@a.test"
    )

    await api_client.post(
        "/org/login", json={"email": b["email"], "password": b["password"]}
    )
    r = await api_client.patch(
        f"/org/api/users/{target['membership_id']}", json={"role": "viewer"}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Phase 3 — invitation hierarchy enforcement
# ---------------------------------------------------------------------------


async def _seed_program(api_session_factory, org_id: int, name: str) -> int:
    from src.models.programs import Program
    async with api_session_factory() as s:
        p = Program(organization_id=org_id, name=name)
        s.add(p)
        await s.commit()
        return p.id


async def _scope_member(
    api_session_factory,
    *,
    user_id: int,
    role: str,
    program_id: int | None = None,
    region_id: int | None = None,
) -> None:
    from src.models.user_organizations import UserOrganization
    async with api_session_factory() as s:
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == user_id,
                UserOrganization.role == role,
            )
            .values(program_id=program_id, region_id=region_id)
        )
        await s.commit()


async def test_org_admin_can_invite_program_manager(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    prog_id = await _seed_program(api_session_factory, org_id, "Prog 1")

    r = await org_admin_client.post(
        "/org/api/users/invite",
        json={
            "email": "pm-new@org.test",
            "role": "program_manager",
            "program_id": prog_id,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "program_manager"
    assert body["program_id"] == prog_id


async def test_program_manager_can_invite_coach_in_their_program(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """program_manager invites a coach without passing program_id —
    the server auto-fills it from the inviter's scope."""
    creds = await seed_org_admin(email="pm@org.test", role="program_manager")
    org_id = creds["organization_id"]
    prog_id = await _seed_program(api_session_factory, org_id, "Prog X")
    await _scope_member(
        api_session_factory,
        user_id=creds["user_id"],
        role="program_manager",
        program_id=prog_id,
    )
    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )

    r = await api_client.post(
        "/org/api/users/invite",
        json={"email": "coach-new@org.test", "role": "coach"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "coach"
    # Coach memberships don't carry program_id directly, but the OrgInvite
    # row stores the program_id so the auto-acceptance flow knows the scope.
    assert body["program_id"] == prog_id


async def test_program_manager_cannot_invite_org_admin(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    creds = await seed_org_admin(email="pm-bad@org.test", role="program_manager")
    prog_id = await _seed_program(
        api_session_factory, creds["organization_id"], "P",
    )
    await _scope_member(
        api_session_factory,
        user_id=creds["user_id"],
        role="program_manager",
        program_id=prog_id,
    )
    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )

    r = await api_client.post(
        "/org/api/users/invite",
        json={"email": "would-be-admin@org.test", "role": "org_admin"},
    )
    assert r.status_code == 404


async def test_program_manager_cannot_invite_another_program_manager(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    creds = await seed_org_admin(email="pm2@org.test", role="program_manager")
    prog_id = await _seed_program(
        api_session_factory, creds["organization_id"], "P",
    )
    await _scope_member(
        api_session_factory,
        user_id=creds["user_id"],
        role="program_manager",
        program_id=prog_id,
    )
    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )

    r = await api_client.post(
        "/org/api/users/invite",
        json={
            "email": "pm-clone@org.test",
            "role": "program_manager",
            "program_id": prog_id,
        },
    )
    assert r.status_code == 404


async def test_program_manager_rejected_cross_program(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """PM cannot invite a region_manager into a sibling program → 404."""
    creds = await seed_org_admin(email="pm3@org.test", role="program_manager")
    org_id = creds["organization_id"]
    prog_a = await _seed_program(api_session_factory, org_id, "A")
    prog_b = await _seed_program(api_session_factory, org_id, "B")
    await _scope_member(
        api_session_factory,
        user_id=creds["user_id"],
        role="program_manager",
        program_id=prog_a.__class__ if False else prog_a,
    )
    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )

    # Try inviting a region_manager whose program_id is B (not A)
    r = await api_client.post(
        "/org/api/users/invite",
        json={
            "email": "rm-cross@org.test",
            "role": "region_manager",
            "program_id": prog_b,
        },
    )
    assert r.status_code == 404


async def test_region_manager_auto_fills_region_when_inviting_coach(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    creds = await seed_org_admin(email="rm-auto@org.test", role="region_manager")
    org_id = creds["organization_id"]
    region_id = await _seed_region(api_session_factory, org_id, name="A")
    await _scope_member(
        api_session_factory,
        user_id=creds["user_id"],
        role="region_manager",
        region_id=region_id,
    )
    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )

    r = await api_client.post(
        "/org/api/users/invite",
        json={"email": "coach-rm@org.test", "role": "coach"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["region_id"] == region_id


async def test_region_manager_cannot_invite_program_manager(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    creds = await seed_org_admin(email="rm-bad@org.test", role="region_manager")
    region_id = await _seed_region(api_session_factory, creds["organization_id"], "R")
    await _scope_member(
        api_session_factory,
        user_id=creds["user_id"],
        role="region_manager",
        region_id=region_id,
    )
    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )

    r = await api_client.post(
        "/org/api/users/invite",
        json={"email": "pm-no@org.test", "role": "program_manager"},
    )
    assert r.status_code == 404


async def test_coach_cannot_reach_invite_endpoint(
    api_client: AsyncClient, seed_org_admin,
):
    creds = await seed_org_admin(email="coach-only@org.test", role="coach")
    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.post(
        "/org/api/users/invite",
        json={"email": "x@org.test", "role": "coach"},
    )
    assert r.status_code == 404


async def test_program_manager_list_users_filters_to_program(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """Phase 12 — a PM sees members whose UserOrganization.program_id matches
    their own program. Region attachment alone no longer implies program
    membership (regions are shared geography)."""
    from src.models.regions import Region

    creds = await seed_org_admin(email="pm-list@org.test", role="program_manager")
    org_id = creds["organization_id"]
    prog_a = await _seed_program(api_session_factory, org_id, "PA")
    prog_b = await _seed_program(api_session_factory, org_id, "PB")
    await _scope_member(
        api_session_factory,
        user_id=creds["user_id"],
        role="program_manager",
        program_id=prog_a,
    )

    # Shared regions — no program_id pinned. Coaches are scoped to a region
    # AND a program separately.
    async with api_session_factory() as s:
        r_a = Region(organization_id=org_id, name="R-A")
        r_b = Region(organization_id=org_id, name="R-B")
        s.add_all([r_a, r_b])
        await s.commit()
        a_id, b_id = r_a.id, r_b.id

    await _seed_member(
        api_session_factory, org_id, email="coach-a@org.test",
        role="coach", region_id=a_id, program_id=prog_a,
    )
    await _seed_member(
        api_session_factory, org_id, email="coach-b@org.test",
        role="coach", region_id=b_id, program_id=prog_b,
    )

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.get("/org/api/users")
    emails = {m["email"] for m in r.json()["members"]}
    assert "coach-a@org.test" in emails
    assert "coach-b@org.test" not in emails, "PM should not see other programs' coaches"
