"""Unit tests for `clamp_and_validate_inviter_authority` (Phase 3).

These exercise the invitation tree + scope-chain enforcement directly,
without going through HTTP. Endpoint-level tests live in
`tests/api/test_org_users.py`.
"""

from __future__ import annotations

import pytest

from src.core.exceptions import NotFoundError
from src.services.org_user_service import (
    ROLE_INVITES_TREE,
    clamp_and_validate_inviter_authority,
)

pytestmark = pytest.mark.asyncio


class _Membership:
    """Lightweight stand-in for UserOrganization — avoids needing a full row."""

    def __init__(
        self,
        *,
        role: str,
        organization_id: int = 1,
        program_id: int | None = None,
        region_id: int | None = None,
        branch_id: int | None = None,
    ) -> None:
        self.role = role
        self.organization_id = organization_id
        self.program_id = program_id
        self.region_id = region_id
        self.branch_id = branch_id


# ---------------------------------------------------------------------------
# Pure-data shape: the tree is the source of truth.
# ---------------------------------------------------------------------------


def test_tree_lists_who_can_invite_what():
    assert "program_manager" in ROLE_INVITES_TREE["org_admin"]
    assert "region_manager" in ROLE_INVITES_TREE["program_manager"]
    assert "coach" in ROLE_INVITES_TREE["region_manager"]
    # Coach + viewer are terminal — they can't invite anyone.
    assert ROLE_INVITES_TREE["coach"] == frozenset()
    assert ROLE_INVITES_TREE["viewer"] == frozenset()
    # Program manager cannot promote above themselves.
    assert "org_admin" not in ROLE_INVITES_TREE["program_manager"]
    assert "program_manager" not in ROLE_INVITES_TREE["program_manager"]
    # Region manager cannot promote above themselves.
    assert "org_admin" not in ROLE_INVITES_TREE["region_manager"]
    assert "program_manager" not in ROLE_INVITES_TREE["region_manager"]
    assert "region_manager" not in ROLE_INVITES_TREE["region_manager"]


# ---------------------------------------------------------------------------
# org_admin — unconstrained
# ---------------------------------------------------------------------------


async def test_org_admin_passes_through_program_id(db_session):
    inviter = _Membership(role="org_admin", organization_id=1)
    prog, region, branch = await clamp_and_validate_inviter_authority(
        db_session,
        inviter=inviter,
        target_role="program_manager",
        program_id=42,
        region_id=None,
        branch_id=None,
    )
    assert prog == 42
    assert region is None
    assert branch is None


# ---------------------------------------------------------------------------
# program_manager — bound to own program
# ---------------------------------------------------------------------------


async def test_pm_cannot_invite_org_admin(db_session):
    inviter = _Membership(role="program_manager", program_id=10)
    with pytest.raises(NotFoundError):
        await clamp_and_validate_inviter_authority(
            db_session,
            inviter=inviter,
            target_role="org_admin",
            program_id=None,
            region_id=None,
            branch_id=None,
        )


async def test_pm_cannot_invite_another_program_manager(db_session):
    inviter = _Membership(role="program_manager", program_id=10)
    with pytest.raises(NotFoundError):
        await clamp_and_validate_inviter_authority(
            db_session,
            inviter=inviter,
            target_role="program_manager",
            program_id=10,
            region_id=None,
            branch_id=None,
        )


async def test_pm_auto_fills_their_own_program_id(db_session):
    inviter = _Membership(role="program_manager", program_id=10)
    prog, region, branch = await clamp_and_validate_inviter_authority(
        db_session,
        inviter=inviter,
        target_role="coach",
        program_id=None,
        region_id=None,
        branch_id=None,
    )
    assert prog == 10
    assert region is None
    assert branch is None


async def test_pm_rejects_cross_program_target(db_session):
    inviter = _Membership(role="program_manager", program_id=10)
    with pytest.raises(NotFoundError):
        await clamp_and_validate_inviter_authority(
            db_session,
            inviter=inviter,
            target_role="region_manager",
            program_id=99,
            region_id=None,
            branch_id=None,
        )


async def test_pm_rejects_region_outside_program(db_session):
    """If PM passes a region_id, that region must belong to PM's program."""
    from src.models.organizations import Organization
    from src.models.programs import Program
    from src.models.regions import Region

    org = Organization(slug="test-pm-region", name="Test")
    db_session.add(org)
    await db_session.flush()
    prog_a = Program(organization_id=org.id, name="Program A")
    prog_b = Program(organization_id=org.id, name="Program B")
    db_session.add_all([prog_a, prog_b])
    await db_session.flush()
    region = Region(organization_id=org.id, name="R-B", program_id=prog_b.id)
    db_session.add(region)
    await db_session.flush()

    inviter = _Membership(
        role="program_manager", organization_id=org.id, program_id=prog_a.id,
    )
    with pytest.raises(NotFoundError):
        await clamp_and_validate_inviter_authority(
            db_session,
            inviter=inviter,
            target_role="region_manager",
            program_id=prog_a.id,
            region_id=region.id,
            branch_id=None,
        )


async def test_pm_accepts_region_inside_program(db_session):
    from src.models.organizations import Organization
    from src.models.programs import Program
    from src.models.regions import Region

    org = Organization(slug="test-pm-region-ok", name="Test")
    db_session.add(org)
    await db_session.flush()
    prog = Program(organization_id=org.id, name="Program A")
    db_session.add(prog)
    await db_session.flush()
    region = Region(organization_id=org.id, name="R-A", program_id=prog.id)
    db_session.add(region)
    await db_session.flush()

    inviter = _Membership(
        role="program_manager", organization_id=org.id, program_id=prog.id,
    )
    out_prog, out_region, _ = await clamp_and_validate_inviter_authority(
        db_session,
        inviter=inviter,
        target_role="region_manager",
        program_id=None,
        region_id=region.id,
        branch_id=None,
    )
    assert out_prog == prog.id
    assert out_region == region.id


# ---------------------------------------------------------------------------
# region_manager — bound to own region
# ---------------------------------------------------------------------------


async def test_rm_cannot_invite_program_manager(db_session):
    inviter = _Membership(role="region_manager", region_id=5)
    with pytest.raises(NotFoundError):
        await clamp_and_validate_inviter_authority(
            db_session,
            inviter=inviter,
            target_role="program_manager",
            program_id=None,
            region_id=None,
            branch_id=None,
        )


async def test_rm_cannot_invite_another_region_manager(db_session):
    inviter = _Membership(role="region_manager", region_id=5)
    with pytest.raises(NotFoundError):
        await clamp_and_validate_inviter_authority(
            db_session,
            inviter=inviter,
            target_role="region_manager",
            program_id=None,
            region_id=5,
            branch_id=None,
        )


async def test_rm_auto_fills_their_own_region_id(db_session):
    inviter = _Membership(role="region_manager", region_id=5)
    _, region, _ = await clamp_and_validate_inviter_authority(
        db_session,
        inviter=inviter,
        target_role="coach",
        program_id=None,
        region_id=None,
        branch_id=None,
    )
    assert region == 5


async def test_rm_rejects_cross_region_target(db_session):
    inviter = _Membership(role="region_manager", region_id=5)
    with pytest.raises(NotFoundError):
        await clamp_and_validate_inviter_authority(
            db_session,
            inviter=inviter,
            target_role="coach",
            program_id=None,
            region_id=99,
            branch_id=None,
        )


# ---------------------------------------------------------------------------
# Terminal roles — must never reach this helper through the routes, but if
# they do, they always 404.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["coach", "viewer"])
async def test_terminal_roles_cannot_invite(db_session, role):
    inviter = _Membership(role=role)
    with pytest.raises(NotFoundError):
        await clamp_and_validate_inviter_authority(
            db_session,
            inviter=inviter,
            target_role="coach",
            program_id=None,
            region_id=None,
            branch_id=None,
        )
