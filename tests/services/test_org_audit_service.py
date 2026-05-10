"""Tests for the org_audit_service.log_org_action contract.

Critical guarantees:
1. Successful writes persist to org_audit_logs.
2. The function NEVER raises — audit failure must not block the action.
3. ip_address and user_agent are extracted from the Request when present.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.org_audit import OrgAuditLog
from src.models.organizations import Organization
from src.services.org_audit_service import log_org_action

pytestmark = pytest.mark.asyncio


def _fake_request(*, ip: str | None = None, ua: str | None = None) -> SimpleNamespace:
    """Lightweight stand-in for fastapi.Request — only the bits the service reads."""
    headers = {}
    if ua is not None:
        headers["user-agent"] = ua
    if ip is not None:
        headers["x-forwarded-for"] = ip
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host="127.0.0.1") if ip is None else None,
    )


async def _seed_org(session: AsyncSession, slug: str) -> Organization:
    o = Organization(slug=slug, name=slug.title())
    session.add(o)
    await session.flush()
    return o


class TestLogOrgAction:
    async def test_writes_a_row(self, db_session: AsyncSession):
        org = await _seed_org(db_session, "audit-test-1")
        await log_org_action(
            db_session,
            organization_id=org.id,
            actor_user_id=None,
            actor_email="admin@example.com",
            action="org.create",
        )
        await db_session.flush()
        rows = list(
            (await db_session.execute(select(OrgAuditLog))).scalars().all()
        )
        assert len(rows) == 1
        assert rows[0].action == "org.create"
        assert rows[0].actor_email == "admin@example.com"

    async def test_normalizes_actor_email_to_lower(self, db_session: AsyncSession):
        org = await _seed_org(db_session, "audit-test-2")
        await log_org_action(
            db_session,
            organization_id=org.id,
            actor_user_id=None,
            actor_email="ADMIN@Example.COM",
            action="org.update",
        )
        await db_session.flush()
        row = (await db_session.execute(select(OrgAuditLog))).scalar_one()
        assert row.actor_email == "admin@example.com"

    async def test_extracts_ip_and_user_agent_from_request(
        self, db_session: AsyncSession
    ):
        org = await _seed_org(db_session, "audit-test-3")
        req = _fake_request(ip="203.0.113.10", ua="Mozilla/5.0 (Test)")
        await log_org_action(
            db_session,
            organization_id=org.id,
            actor_user_id=42,
            actor_email="user@example.com",
            action="auth.org.login",
            request=req,  # type: ignore[arg-type]
        )
        await db_session.flush()
        row = (await db_session.execute(select(OrgAuditLog))).scalar_one()
        assert row.ip_address == "203.0.113.10"
        assert row.user_agent == "Mozilla/5.0 (Test)"

    async def test_never_raises_on_invalid_org_id(self, db_session: AsyncSession):
        # FK to organizations enforces a real org; passing a non-existent id
        # would normally raise on flush. The service must swallow the error
        # so audit failure never blocks the underlying request.
        await log_org_action(
            db_session,
            organization_id=999_999,
            actor_user_id=None,
            actor_email="admin@example.com",
            action="org.create",
        )
        # If we got here without an exception, the contract is upheld.
