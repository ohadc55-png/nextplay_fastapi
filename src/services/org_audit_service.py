"""Org audit logging service — immutable trail of state-changing actions.

Every Org Admin / System Admin endpoint that mutates org-scoped data calls
`log_org_action`. Audit failure must NEVER block the underlying action: this
service catches and swallows errors after logging them.

For System Admin actions: `actor_user_id=None`, `actor_email=<admin email>`.
For Org Admin actions: both populated from `request.state.user`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.org_audit import OrgAuditLog
from src.repositories.org_audit_repo import OrgAuditRepository

logger = logging.getLogger(__name__)


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _user_agent(request: Request | None) -> str | None:
    return request.headers.get("user-agent") if request else None


async def log_org_action(
    session: AsyncSession,
    *,
    organization_id: int,
    actor_user_id: int | None,
    actor_email: str | None,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    request: Request | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one row to org_audit_logs. Never raises — audit failures are
    logged via the standard logger and otherwise swallowed."""
    try:
        repo = OrgAuditRepository(session)
        log = OrgAuditLog(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            actor_email=(actor_email or "").lower() or None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
            attributes_json=extra,
        )
        await repo.add(log)
    except Exception as exc:  # pragma: no cover — defensive
        logger.error(
            "[org_audit] failed to log action=%s org=%s: %s",
            action, organization_id, exc, exc_info=True,
        )


__all__ = ["log_org_action"]
