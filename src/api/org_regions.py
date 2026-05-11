"""Region CRUD under /org/api/regions/* (Phase 1.3).

Auth: org session via `get_current_org_membership`. Role gates:
- LIST   GET    /org/api/regions          → org_admin sees all; region_manager
                                            sees only their own region
- CREATE POST   /org/api/regions          → org_admin
- READ   GET    /org/api/regions/{id}     → any active member of the org
                                            (404 cross-org per the standard rule)
- UPDATE PATCH  /org/api/regions/{id}     → org_admin
- DELETE DELETE /org/api/regions/{id}     → org_admin; refuses with 409 if
                                            any branch still references the region

404-not-403 throughout — cross-org or wrong-role hits return NotFoundError(404).
Every state-changing operation logs an OrgAuditLog row.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership, require_role
from src.core.database import get_db
from src.core.exceptions import ConflictError, NotFoundError
from src.models.branches import Branch
from src.models.players import Player
from src.models.regions import Region
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.models.users import User
from src.repositories.regions_repo import RegionsRepository
from src.schemas.regions import RegionCreate, RegionOut, RegionUpdate
from src.services.org_audit_service import log_org_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/regions", tags=["org-regions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _org_ctx(membership: UserOrganization) -> tuple[int, str, int | None]:
    """Pull (org_id, role, scoped_region_id) off a validated membership."""
    return membership.organization_id, membership.role, membership.region_id


def _ensure_region_visible(
    region: Region | None, membership: UserOrganization
) -> Region:
    """404 if region is None OR (region_manager AND region != own). Centralizes
    the cross-org / out-of-scope guard."""
    if region is None or region.organization_id != membership.organization_id:
        raise NotFoundError("Region not found")
    if (
        membership.role == "region_manager"
        and region.id != membership.region_id
    ):
        raise NotFoundError("Region not found")
    return region


# ---------------------------------------------------------------------------
# GET /org/api/regions — list
# ---------------------------------------------------------------------------


async def _enrich_regions(
    db: AsyncSession, org_id: int, region_ids: list[int],
) -> dict[int, dict]:
    """One-shot batch fetch of per-region aggregates. Returns a dict keyed
    by region_id: {branch_count, team_count, coach_count, player_count,
    manager_name}. Five queries total — independent of how many regions
    the org has."""
    if not region_ids:
        return {}

    # 1) branches per region
    branch_rows = (await db.execute(
        select(Branch.region_id, func.count(Branch.id))
        .where(Branch.organization_id == org_id, Branch.region_id.in_(region_ids))
        .group_by(Branch.region_id)
    )).all()
    branch_count = {rid: int(c) for (rid, c) in branch_rows}

    # 2) teams per region (via team.branch_id → branch.region_id)
    team_rows = (await db.execute(
        select(Branch.region_id, func.count(TeamProfile.id))
        .join(TeamProfile, TeamProfile.branch_id == Branch.id)
        .where(
            TeamProfile.organization_id == org_id,
            Branch.region_id.in_(region_ids),
        )
        .group_by(Branch.region_id)
    )).all()
    team_count = {rid: int(c) for (rid, c) in team_rows}

    # 3) coaches per region (active coach memberships scoped to branches
    #    of that region). The seeder pins coach.branch_id to their team's
    #    branch, so this is the source of truth.
    coach_rows = (await db.execute(
        select(Branch.region_id, func.count(UserOrganization.id))
        .join(Branch, Branch.id == UserOrganization.branch_id)
        .where(
            UserOrganization.organization_id == org_id,
            UserOrganization.role == "coach",
            UserOrganization.status == "active",
            Branch.region_id.in_(region_ids),
        )
        .group_by(Branch.region_id)
    )).all()
    coach_count = {rid: int(c) for (rid, c) in coach_rows}

    # 4) active players per region (via team_profile → branch.region_id)
    player_rows = (await db.execute(
        select(Branch.region_id, func.count(Player.id))
        .join(TeamProfile, TeamProfile.id == Player.team_id)
        .join(Branch, Branch.id == TeamProfile.branch_id)
        .where(
            Player.organization_id == org_id,
            Player.active.is_(True),
            Branch.region_id.in_(region_ids),
        )
        .group_by(Branch.region_id)
    )).all()
    player_count = {rid: int(c) for (rid, c) in player_rows}

    # 5) region_manager name per region. A region may have 0 or many; we
    #    pick the first active membership ordered by id for determinism.
    mgr_rows = (await db.execute(
        select(
            UserOrganization.region_id,
            User.display_name,
            User.email,
            UserOrganization.id,
        )
        .join(User, User.id == UserOrganization.user_id)
        .where(
            UserOrganization.organization_id == org_id,
            UserOrganization.role == "region_manager",
            UserOrganization.status == "active",
            UserOrganization.region_id.in_(region_ids),
        )
        .order_by(UserOrganization.id)
    )).all()
    manager_name: dict[int, str] = {}
    for region_id, display, email, _uo_id in mgr_rows:
        if region_id not in manager_name:
            manager_name[region_id] = display or email

    return {
        rid: {
            "branch_count": branch_count.get(rid, 0),
            "team_count": team_count.get(rid, 0),
            "coach_count": coach_count.get(rid, 0),
            "player_count": player_count.get(rid, 0),
            "manager_name": manager_name.get(rid),
        }
        for rid in region_ids
    }


@router.get("", response_model=dict)
async def list_regions(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    org_id, role, scoped_region_id = _org_ctx(membership)

    repo = RegionsRepository(db)
    rows = await repo.list_for_org(org_id)

    # region_manager only sees their own region.
    if role == "region_manager" and scoped_region_id is not None:
        rows = [r for r in rows if r.id == scoped_region_id]

    rows.sort(key=lambda r: r.name)

    enrichment = await _enrich_regions(db, org_id, [r.id for r in rows])
    out = []
    for r in rows:
        base = RegionOut.model_validate(r).model_dump(mode="json")
        base.update(enrichment.get(r.id, {}))
        out.append(base)
    return {"regions": out}


# ---------------------------------------------------------------------------
# POST /org/api/regions — create (org_admin only)
# ---------------------------------------------------------------------------


@router.post("", response_model=RegionOut, status_code=status.HTTP_201_CREATED)
async def create_region(
    body: RegionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(require_role("org_admin")),
) -> RegionOut:
    org_id = membership.organization_id
    name = body.name.strip()

    repo = RegionsRepository(db)
    if await repo.get_by_name(organization_id=org_id, name=name) is not None:
        raise ConflictError("A region with this name already exists.", code="duplicate_name")

    region = Region(organization_id=org_id, name=name)
    try:
        region = await repo.create(region)
    except IntegrityError as e:
        # Race: another writer beat us to the uq_regions_org_name constraint.
        await db.rollback()
        raise ConflictError(
            "A region with this name already exists.", code="duplicate_name"
        ) from e

    user = request.state.user
    await log_org_action(
        db,
        organization_id=org_id,
        actor_user_id=user.id,
        actor_email=user.email,
        action="region.create",
        target_type="region",
        target_id=region.id,
        request=request,
        extra={"name": region.name},
    )
    return RegionOut.model_validate(region)


# ---------------------------------------------------------------------------
# GET /org/api/regions/{region_id} — detail
# ---------------------------------------------------------------------------


@router.get("/{region_id}", response_model=RegionOut)
async def get_region(
    region_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> RegionOut:
    region = await RegionsRepository(db).get_for_org(
        region_id, membership.organization_id
    )
    _ensure_region_visible(region, membership)
    return RegionOut.model_validate(region)


# ---------------------------------------------------------------------------
# PATCH /org/api/regions/{region_id} — update (org_admin only)
# ---------------------------------------------------------------------------


@router.patch("/{region_id}", response_model=RegionOut)
async def update_region(
    region_id: int,
    body: RegionUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(require_role("org_admin")),
) -> RegionOut:
    repo = RegionsRepository(db)
    region = await repo.get_for_org(region_id, membership.organization_id)
    if region is None:
        raise NotFoundError("Region not found")

    changes: dict = {}
    if body.name is not None and body.name.strip() != region.name:
        new_name = body.name.strip()
        # uniqueness pre-check (concurrent writes still need the constraint).
        existing = await repo.get_by_name(
            organization_id=membership.organization_id, name=new_name
        )
        if existing is not None and existing.id != region.id:
            raise ConflictError(
                "A region with this name already exists.", code="duplicate_name"
            )
        changes["name"] = {"from": region.name, "to": new_name}
        region.name = new_name

    if changes:
        try:
            await repo.update(region)
        except IntegrityError as e:
            await db.rollback()
            raise ConflictError(
                "A region with this name already exists.", code="duplicate_name"
            ) from e
        # Server-default onupdate=now() bumps updated_at at the DB level —
        # refresh so the response includes the new value (otherwise the
        # attribute is expired and Pydantic's model_validate triggers a
        # MissingGreenlet on async).
        await db.refresh(region)

        user = request.state.user
        await log_org_action(
            db,
            organization_id=membership.organization_id,
            actor_user_id=user.id,
            actor_email=user.email,
            action="region.update",
            target_type="region",
            target_id=region.id,
            request=request,
            extra=changes,
        )
    return RegionOut.model_validate(region)


# ---------------------------------------------------------------------------
# DELETE /org/api/regions/{region_id} — delete (org_admin only)
# ---------------------------------------------------------------------------


@router.delete("/{region_id}", response_model=dict)
async def delete_region(
    region_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(require_role("org_admin")),
) -> dict:
    repo = RegionsRepository(db)
    region = await repo.get_for_org(region_id, membership.organization_id)
    if region is None:
        raise NotFoundError("Region not found")

    # Refuse to delete if branches still point at this region. Branches'
    # region_id ON DELETE SET NULL would otherwise silently orphan them.
    branch_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Branch)
                .where(Branch.region_id == region.id)
            )
        ).scalar()
        or 0
    )
    if branch_count > 0:
        raise ConflictError(
            f"Region has {branch_count} branch(es) attached. "
            "Move or delete them first.",
            code="region_has_branches",
        )

    name_snapshot = region.name
    await repo.delete(region)

    user = request.state.user
    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=user.id,
        actor_email=user.email,
        action="region.delete",
        target_type="region",
        target_id=region_id,
        request=request,
        extra={"name": name_snapshot},
    )
    return {"ok": True}


__all__ = ["router"]
