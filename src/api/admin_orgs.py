"""System Admin endpoints for organization management.

JSON-only — the admin HTML pages for orgs are deferred to Phase 1 (matches
the existing pattern where /admin/api/* JSON shipped before /admin/*.html).

All endpoints `Depends(get_current_admin)` — System Admin (Ohad) is the only
actor who can create/destroy orgs or assign org-admin memberships.

Audit: every state-changing endpoint logs an OrgAuditLog row via
`log_org_action`. System-admin actions have `actor_user_id=None` and
`actor_email=request.session["admin_email"]`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_admin
from src.core.database import get_db
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.models.organizations import Organization
from src.models.user_organizations import UserOrganization
from src.repositories.organizations_repo import OrganizationsRepository
from src.repositories.user_organizations_repo import UserOrganizationsRepository
from src.repositories.users_repo import UsersRepository
from src.services.org_audit_service import log_org_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api", tags=["admin-orgs"])

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")


# ---------------------------------------------------------------------------
# Request / response shapes (System-admin-only — kept local to this module)
# ---------------------------------------------------------------------------


class OrgCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    plan: str = "enterprise"
    status: str = "active"


class OrgUpdateRequest(BaseModel):
    name: str | None = None
    status: str | None = None
    plan: str | None = None


class OrgAdminAssignRequest(BaseModel):
    user_id: int


class OrgListItem(BaseModel):
    id: int
    slug: str
    name: str
    status: str
    plan: str
    member_count: int
    created_at: datetime | None = None
    deleted_at: datetime | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _count_members(db: AsyncSession, org_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(UserOrganization)
        .where(
            UserOrganization.organization_id == org_id,
            UserOrganization.status == "active",
        )
    )
    return int((await db.execute(stmt)).scalar() or 0)


def _validate_slug(slug: str) -> str:
    s = slug.strip().lower()
    if not _SLUG_RE.match(s):
        raise ValidationError(
            "Slug must be 1-50 chars, lowercase alphanumeric + hyphens, "
            "no leading or trailing hyphen.",
            code="invalid_slug",
        )
    return s


# ---------------------------------------------------------------------------
# GET /admin/api/orgs — list (System Admin)
# ---------------------------------------------------------------------------


@router.get("/orgs")
async def admin_orgs_list(
    _email: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    include_archived: bool = False,
) -> dict:
    """List all orgs. By default hides soft-deleted ones — pass
    `?include_archived=true` to see them too."""
    stmt = select(Organization)
    if not include_archived:
        stmt = stmt.where(Organization.deleted_at.is_(None))
    stmt = stmt.order_by(Organization.name)
    rows = list((await db.execute(stmt)).scalars().all())
    out: list[dict] = []
    for org in rows:
        out.append(
            OrgListItem(
                id=org.id,
                slug=org.slug,
                name=org.name,
                status=org.status,
                plan=org.plan,
                member_count=await _count_members(db, org.id),
                created_at=org.created_at,
                deleted_at=org.deleted_at,
            ).model_dump(mode="json")
        )
    return {"organizations": out, "total": len(out)}


# ---------------------------------------------------------------------------
# POST /admin/api/orgs — create (System Admin)
# ---------------------------------------------------------------------------


@router.post("/orgs", status_code=201)
async def admin_orgs_create(
    body: OrgCreateRequest,
    request: Request,
    email: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    slug = _validate_slug(body.slug)
    repo = OrganizationsRepository(db)
    if await repo.get_by_slug(slug) is not None:
        raise ConflictError("An organization with this slug already exists.")

    org = Organization(
        slug=slug,
        name=body.name.strip(),
        status=body.status,
        plan=body.plan,
    )
    org = await repo.create(org)
    await log_org_action(
        db,
        organization_id=org.id,
        actor_user_id=None,
        actor_email=email,
        action="org.create",
        target_type="organization",
        target_id=org.id,
        request=request,
        extra={"slug": org.slug, "name": org.name},
    )
    return {"id": org.id, "slug": org.slug, "name": org.name}


# ---------------------------------------------------------------------------
# GET /admin/api/orgs/{org_id} — details
# ---------------------------------------------------------------------------


@router.get("/orgs/{org_id}")
async def admin_orgs_get(
    org_id: int,
    _email: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org = await OrganizationsRepository(db).get(org_id)
    if not org:
        raise NotFoundError("Organization not found")
    return {
        "id": org.id,
        "slug": org.slug,
        "name": org.name,
        "status": org.status,
        "plan": org.plan,
        "deleted_at": org.deleted_at.isoformat() if org.deleted_at else None,
        "created_at": org.created_at.isoformat() if org.created_at else None,
        "member_count": await _count_members(db, org.id),
    }


# ---------------------------------------------------------------------------
# PATCH /admin/api/orgs/{org_id} — update name / status / plan
# ---------------------------------------------------------------------------


@router.patch("/orgs/{org_id}")
async def admin_orgs_update(
    org_id: int,
    body: OrgUpdateRequest,
    request: Request,
    email: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    repo = OrganizationsRepository(db)
    org = await repo.get(org_id)
    if not org:
        raise NotFoundError("Organization not found")

    changes: dict = {}
    if body.name is not None and body.name != org.name:
        changes["name"] = {"from": org.name, "to": body.name}
        org.name = body.name.strip()
    if body.status is not None and body.status != org.status:
        changes["status"] = {"from": org.status, "to": body.status}
        org.status = body.status
    if body.plan is not None and body.plan != org.plan:
        changes["plan"] = {"from": org.plan, "to": body.plan}
        org.plan = body.plan

    if changes:
        await repo.update(org)
        await log_org_action(
            db,
            organization_id=org.id,
            actor_user_id=None,
            actor_email=email,
            action="org.update",
            target_type="organization",
            target_id=org.id,
            request=request,
            extra=changes,
        )
    return {"id": org.id, "name": org.name, "status": org.status, "plan": org.plan}


# ---------------------------------------------------------------------------
# DELETE /admin/api/orgs/{org_id} — soft-delete
# ---------------------------------------------------------------------------


@router.delete("/orgs/{org_id}")
async def admin_orgs_soft_delete(
    org_id: int,
    request: Request,
    email: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    repo = OrganizationsRepository(db)
    if not await repo.soft_delete(org_id):
        raise NotFoundError("Organization not found")
    await log_org_action(
        db,
        organization_id=org_id,
        actor_user_id=None,
        actor_email=email,
        action="org.delete",
        target_type="organization",
        target_id=org_id,
        request=request,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /admin/api/orgs/{org_id}/admins — assign org_admin role
# ---------------------------------------------------------------------------


@router.post("/orgs/{org_id}/admins", status_code=201)
async def admin_orgs_assign_admin(
    org_id: int,
    body: OrgAdminAssignRequest,
    request: Request,
    email: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Promote an existing user to `org_admin` of the given org. Idempotent:
    re-assigning an existing membership returns it unchanged."""
    org_repo = OrganizationsRepository(db)
    org = await org_repo.get(org_id)
    if not org or org.deleted_at is not None:
        raise NotFoundError("Organization not found")

    user = await UsersRepository(db).get_active(body.user_id)
    if not user:
        raise NotFoundError("User not found")

    uo_repo = UserOrganizationsRepository(db)
    existing = await uo_repo.get_active(
        user_id=user.id, organization_id=org.id, role="org_admin",
    )
    if existing:
        return {"id": existing.id, "user_id": user.id, "role": "org_admin"}

    membership = UserOrganization(
        user_id=user.id, organization_id=org.id, role="org_admin", status="active",
    )
    await uo_repo.create(membership)
    await log_org_action(
        db,
        organization_id=org.id,
        actor_user_id=None,
        actor_email=email,
        action="org.admin.assign",
        target_type="user",
        target_id=user.id,
        request=request,
        extra={"role": "org_admin"},
    )
    return {"id": membership.id, "user_id": user.id, "role": "org_admin"}


# ---------------------------------------------------------------------------
# DELETE /admin/api/orgs/{org_id}/admins/{user_id} — revoke org_admin
# ---------------------------------------------------------------------------


@router.delete("/orgs/{org_id}/admins/{user_id}")
async def admin_orgs_revoke_admin(
    org_id: int,
    user_id: int,
    request: Request,
    email: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    repo = UserOrganizationsRepository(db)
    membership = await repo.get_active(
        user_id=user_id, organization_id=org_id, role="org_admin",
    )
    if not membership:
        raise NotFoundError("Membership not found")
    membership.status = "removed"
    await repo.update(membership)
    await log_org_action(
        db,
        organization_id=org_id,
        actor_user_id=None,
        actor_email=email,
        action="org.admin.remove",
        target_type="user",
        target_id=user_id,
        request=request,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# GET /admin/api/orgs/{org_id}/audit — read audit trail
# ---------------------------------------------------------------------------


@router.get("/orgs/{org_id}/audit")
async def admin_orgs_audit(
    org_id: int,
    _email: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
    offset: int = 0,
) -> dict:
    org = await OrganizationsRepository(db).get(org_id)
    if not org:
        raise NotFoundError("Organization not found")

    from src.repositories.org_audit_repo import OrgAuditRepository

    rows = await OrgAuditRepository(db).list_for_org(
        org_id, limit=max(1, min(limit, 500)), offset=max(0, offset),
    )
    return {
        "audit_log": [
            {
                "id": r.id,
                "actor_email": r.actor_email,
                "actor_user_id": r.actor_user_id,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "ip_address": r.ip_address,
                "extra": r.attributes_json,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "limit": limit,
        "offset": offset,
    }


__all__ = ["router"]
