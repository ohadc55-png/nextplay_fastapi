"""Branch CRUD under /org/api/branches/* (Phase 1.3).

Auth: org session via `get_current_org_membership`. Role gates:
- LIST   GET    /org/api/branches         → org_admin all; region_manager scoped
                                            to own region; branch_manager only
                                            their one branch
- CREATE POST   /org/api/branches         → org_admin (any region), region_manager
                                            (must be in own region — auto-forced)
- READ   GET    /org/api/branches/{id}    → scoped per role
- UPDATE PATCH  /org/api/branches/{id}    → org_admin OR region_manager-of-region
- DELETE DELETE /org/api/branches/{id}    → org_admin; refuses 409 if teams
                                            still attached

App-layer rule (from branches.py docstring): when region_id is set, that
region's organization_id MUST equal the branch's organization_id. Enforced
in `_validate_region_belongs_to_org`.

404-not-403 throughout. Audit log on every state change.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, status
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
from src.repositories.branches_repo import BranchesRepository
from src.schemas.branches import BranchCreate, BranchOut, BranchUpdate
from src.services.org_audit_service import log_org_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/branches", tags=["org-branches"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _validate_region_belongs_to_org(
    db: AsyncSession, *, org_id: int, region_id: int | None
) -> None:
    """If region_id is set, the region must belong to org_id. Otherwise 404
    (treats cross-org region IDs as 'not found' per the 404 rule)."""
    if region_id is None:
        return
    region = (
        await db.execute(
            select(Region).where(
                Region.id == region_id, Region.organization_id == org_id
            )
        )
    ).scalar_one_or_none()
    if region is None:
        raise NotFoundError("Region not found")


def _ensure_branch_visible(
    branch: Branch | None, membership: UserOrganization
) -> Branch:
    """Scope guard for read paths. 404 if branch is missing OR
    region_manager hits a branch outside their region OR
    branch_manager hits a branch other than their own."""
    if branch is None or branch.organization_id != membership.organization_id:
        raise NotFoundError("Branch not found")
    role = membership.role
    if role == "region_manager":
        if branch.region_id != membership.region_id:
            raise NotFoundError("Branch not found")
    if role == "branch_manager":
        if branch.id != membership.branch_id:
            raise NotFoundError("Branch not found")
    return branch


def _force_region_scope(
    body_region_id: int | None, membership: UserOrganization
) -> int | None:
    """region_manager creates can ONLY target their own region. If the body
    omits region_id (or sets it to a different one), force it to their region."""
    if membership.role == "region_manager":
        if (
            body_region_id is not None
            and body_region_id != membership.region_id
        ):
            raise NotFoundError("Region not found")
        return membership.region_id
    return body_region_id


# ---------------------------------------------------------------------------
# GET /org/api/branches — list
# ---------------------------------------------------------------------------


async def _enrich_branches(
    db: AsyncSession, org_id: int, branch_ids: list[int],
) -> dict[int, dict]:
    """Per-branch aggregates: team_count + player_count. Two queries."""
    if not branch_ids:
        return {}

    team_rows = (await db.execute(
        select(TeamProfile.branch_id, func.count(TeamProfile.id))
        .where(
            TeamProfile.organization_id == org_id,
            TeamProfile.branch_id.in_(branch_ids),
        )
        .group_by(TeamProfile.branch_id)
    )).all()
    team_count = {bid: int(c) for (bid, c) in team_rows}

    player_rows = (await db.execute(
        select(TeamProfile.branch_id, func.count(Player.id))
        .join(Player, Player.team_id == TeamProfile.id)
        .where(
            Player.organization_id == org_id,
            Player.active.is_(True),
            TeamProfile.branch_id.in_(branch_ids),
        )
        .group_by(TeamProfile.branch_id)
    )).all()
    player_count = {bid: int(c) for (bid, c) in player_rows}

    return {
        bid: {
            "team_count": team_count.get(bid, 0),
            "player_count": player_count.get(bid, 0),
        }
        for bid in branch_ids
    }


@router.get("", response_model=dict)
async def list_branches(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
    region_id: int | None = Query(default=None),
) -> dict:
    org_id = membership.organization_id
    role = membership.role

    repo = BranchesRepository(db)
    if region_id is not None:
        rows = await repo.list_for_region(
            organization_id=org_id, region_id=region_id
        )
    else:
        rows = await repo.list_for_org(org_id)

    # Scope filtering.
    if role == "region_manager" and membership.region_id is not None:
        rows = [b for b in rows if b.region_id == membership.region_id]
    elif role == "branch_manager" and membership.branch_id is not None:
        rows = [b for b in rows if b.id == membership.branch_id]

    rows.sort(key=lambda b: b.name)

    enrichment = await _enrich_branches(db, org_id, [b.id for b in rows])
    out = []
    for b in rows:
        base = BranchOut.model_validate(b).model_dump(mode="json")
        base.update(enrichment.get(b.id, {}))
        out.append(base)
    return {"branches": out}


# ---------------------------------------------------------------------------
# POST /org/api/branches — create (org_admin OR region_manager)
# ---------------------------------------------------------------------------


@router.post("", response_model=BranchOut, status_code=status.HTTP_201_CREATED)
async def create_branch(
    body: BranchCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager")
    ),
) -> BranchOut:
    org_id = membership.organization_id
    name = body.name.strip()

    target_region_id = _force_region_scope(body.region_id, membership)
    await _validate_region_belongs_to_org(
        db, org_id=org_id, region_id=target_region_id
    )

    repo = BranchesRepository(db)
    if await repo.get_by_name(organization_id=org_id, name=name) is not None:
        raise ConflictError(
            "A branch with this name already exists.", code="duplicate_name"
        )

    branch = Branch(
        organization_id=org_id, region_id=target_region_id, name=name
    )
    try:
        branch = await repo.create(branch)
    except IntegrityError as e:
        await db.rollback()
        raise ConflictError(
            "A branch with this name already exists.", code="duplicate_name"
        ) from e

    user = request.state.user
    await log_org_action(
        db,
        organization_id=org_id,
        actor_user_id=user.id,
        actor_email=user.email,
        action="branch.create",
        target_type="branch",
        target_id=branch.id,
        request=request,
        extra={"name": branch.name, "region_id": branch.region_id},
    )
    return BranchOut.model_validate(branch)


# ---------------------------------------------------------------------------
# GET /org/api/branches/{branch_id} — detail
# ---------------------------------------------------------------------------


@router.get("/{branch_id}", response_model=BranchOut)
async def get_branch(
    branch_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> BranchOut:
    branch = await BranchesRepository(db).get_for_org(
        branch_id, membership.organization_id
    )
    _ensure_branch_visible(branch, membership)
    # Enrich the single-branch payload too — drives the detail page tiles.
    enrichment = await _enrich_branches(db, membership.organization_id, [branch.id])
    payload = BranchOut.model_validate(branch).model_dump(mode="json")
    payload.update(enrichment.get(branch.id, {}))
    return BranchOut.model_validate(payload)


# ---------------------------------------------------------------------------
# PATCH /org/api/branches/{branch_id} — update
# ---------------------------------------------------------------------------


@router.patch("/{branch_id}", response_model=BranchOut)
async def update_branch(
    branch_id: int,
    body: BranchUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager")
    ),
) -> BranchOut:
    repo = BranchesRepository(db)
    branch = await repo.get_for_org(branch_id, membership.organization_id)
    if branch is None:
        raise NotFoundError("Branch not found")

    # region_manager can only update branches inside their own region.
    if (
        membership.role == "region_manager"
        and branch.region_id != membership.region_id
    ):
        raise NotFoundError("Branch not found")

    changes: dict = {}

    if body.name is not None and body.name.strip() != branch.name:
        new_name = body.name.strip()
        existing = await repo.get_by_name(
            organization_id=membership.organization_id, name=new_name
        )
        if existing is not None and existing.id != branch.id:
            raise ConflictError(
                "A branch with this name already exists.", code="duplicate_name"
            )
        changes["name"] = {"from": branch.name, "to": new_name}
        branch.name = new_name

    if "region_id" in body.model_fields_set:
        target_region_id = body.region_id
        # region_manager cannot move a branch out of their region.
        if membership.role == "region_manager":
            if (
                target_region_id is not None
                and target_region_id != membership.region_id
            ):
                raise NotFoundError("Region not found")
            target_region_id = membership.region_id
        await _validate_region_belongs_to_org(
            db,
            org_id=membership.organization_id,
            region_id=target_region_id,
        )
        if target_region_id != branch.region_id:
            changes["region_id"] = {
                "from": branch.region_id,
                "to": target_region_id,
            }
            branch.region_id = target_region_id

    if changes:
        try:
            await repo.update(branch)
        except IntegrityError as e:
            await db.rollback()
            raise ConflictError(
                "A branch with this name already exists.", code="duplicate_name"
            ) from e
        # Refresh to pull the server-side updated_at (see same comment in
        # org_regions.update_region).
        await db.refresh(branch)

        user = request.state.user
        await log_org_action(
            db,
            organization_id=membership.organization_id,
            actor_user_id=user.id,
            actor_email=user.email,
            action="branch.update",
            target_type="branch",
            target_id=branch.id,
            request=request,
            extra=changes,
        )
    return BranchOut.model_validate(branch)


# ---------------------------------------------------------------------------
# DELETE /org/api/branches/{branch_id} — delete (org_admin only)
# ---------------------------------------------------------------------------


@router.delete("/{branch_id}", response_model=dict)
async def delete_branch(
    branch_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(require_role("org_admin")),
) -> dict:
    repo = BranchesRepository(db)
    branch = await repo.get_for_org(branch_id, membership.organization_id)
    if branch is None:
        raise NotFoundError("Branch not found")

    # Refuse if teams still pointing at this branch.
    team_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(TeamProfile)
                .where(TeamProfile.branch_id == branch.id)
            )
        ).scalar()
        or 0
    )
    if team_count > 0:
        raise ConflictError(
            f"Branch has {team_count} team(s) attached. "
            "Move or delete them first.",
            code="branch_has_teams",
        )

    name_snapshot = branch.name
    region_snapshot = branch.region_id
    await repo.delete(branch)

    user = request.state.user
    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=user.id,
        actor_email=user.email,
        action="branch.delete",
        target_type="branch",
        target_id=branch_id,
        request=request,
        extra={"name": name_snapshot, "region_id": region_snapshot},
    )
    return {"ok": True}


__all__ = ["router"]
