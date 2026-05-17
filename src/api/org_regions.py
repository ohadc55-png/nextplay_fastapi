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
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership, require_role
from src.core.database import get_db
from src.core.exceptions import ConflictError, NotFoundError
from src.models.branches import Branch
from src.models.players import Player
from src.models.practice_sessions import PracticeSession
from src.models.programs import Program
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
    manager_name, program_name}. Six queries total — independent of how
    many regions the org has.

    Team and player counts honor BOTH the new direct `team.region_id` FK
    and the legacy `team.branch_id -> branch.region_id` path. The two
    sub-queries are unioned by membership_id to avoid double-counting a
    team that happens to have both a region_id AND a branch_id set.
    """
    if not region_ids:
        return {}

    # 1) branches per region (legacy axis — kept for back-compat customers)
    branch_rows = (await db.execute(
        select(Branch.region_id, func.count(Branch.id))
        .where(Branch.organization_id == org_id, Branch.region_id.in_(region_ids))
        .group_by(Branch.region_id)
    )).all()
    branch_count = {rid: int(c) for (rid, c) in branch_rows}

    # 2) Teams that belong to each region by EITHER the new direct FK OR
    #    the legacy branch path. SQLite's GROUP BY doesn't deduplicate
    #    cross-join hits, so we collect team_ids with their effective
    #    region_id via two SELECTs and union in Python (small payload).
    direct_team_rows = (await db.execute(
        select(TeamProfile.region_id, TeamProfile.id)
        .where(
            TeamProfile.organization_id == org_id,
            TeamProfile.region_id.in_(region_ids),
        )
    )).all()
    legacy_team_rows = (await db.execute(
        select(Branch.region_id, TeamProfile.id)
        .join(TeamProfile, TeamProfile.branch_id == Branch.id)
        .where(
            TeamProfile.organization_id == org_id,
            Branch.region_id.in_(region_ids),
        )
    )).all()
    teams_by_region: dict[int, set[int]] = {}
    for rid, tid in direct_team_rows:
        teams_by_region.setdefault(rid, set()).add(tid)
    for rid, tid in legacy_team_rows:
        teams_by_region.setdefault(rid, set()).add(tid)
    team_count = {rid: len(s) for rid, s in teams_by_region.items()}

    # 3) coaches per region (active coach memberships scoped to either
    #    region_id directly OR a branch in that region — same dual path).
    direct_coach_rows = (await db.execute(
        select(UserOrganization.region_id, func.count(UserOrganization.id))
        .where(
            UserOrganization.organization_id == org_id,
            UserOrganization.role == "coach",
            UserOrganization.status == "active",
            UserOrganization.region_id.in_(region_ids),
        )
        .group_by(UserOrganization.region_id)
    )).all()
    legacy_coach_rows = (await db.execute(
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
    coach_count: dict[int, int] = {}
    for rid, c in direct_coach_rows:
        coach_count[rid] = coach_count.get(rid, 0) + int(c)
    for rid, c in legacy_coach_rows:
        coach_count[rid] = coach_count.get(rid, 0) + int(c)

    # 4) active players per region — across the same union of team paths.
    if any(teams_by_region.values()):
        all_team_ids = {tid for s in teams_by_region.values() for tid in s}
        team_to_region: dict[int, int] = {}
        for rid, s in teams_by_region.items():
            for tid in s:
                team_to_region[tid] = rid
        player_rows = (await db.execute(
            select(Player.team_id, func.count(Player.id))
            .where(
                Player.organization_id == org_id,
                Player.active.is_(True),
                Player.team_id.in_(all_team_ids),
            )
            .group_by(Player.team_id)
        )).all()
        player_count: dict[int, int] = {}
        for tid, c in player_rows:
            rid = team_to_region.get(tid)
            if rid is not None:
                player_count[rid] = player_count.get(rid, 0) + int(c)
    else:
        player_count = {}

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

    # 6) Phase 12 — regions are shared across programs, so "parent program"
    #    is no longer 1:1. Surface a comma-joined preview of program names
    #    whose teams live in this region (capped at 3 for readability).
    program_rows = (await db.execute(
        select(TeamProfile.region_id, Program.name)
        .join(Program, Program.id == TeamProfile.program_id)
        .where(
            TeamProfile.organization_id == org_id,
            TeamProfile.region_id.in_(region_ids),
        )
        .distinct()
    )).all()
    programs_by_region: dict[int, list[str]] = {}
    for rid, pname in program_rows:
        if pname:
            programs_by_region.setdefault(rid, []).append(pname)
    program_name: dict[int, str | None] = {}
    for rid, names in programs_by_region.items():
        names.sort()
        if len(names) <= 3:
            program_name[rid] = " · ".join(names)
        else:
            program_name[rid] = " · ".join(names[:3]) + f" +{len(names) - 3}"

    return {
        rid: {
            "branch_count": branch_count.get(rid, 0),
            "team_count": team_count.get(rid, 0),
            "coach_count": coach_count.get(rid, 0),
            "player_count": player_count.get(rid, 0),
            "manager_name": manager_name.get(rid),
            "program_name": program_name.get(rid),
        }
        for rid in region_ids
    }


@router.get("", response_model=dict)
async def list_regions(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
    program_id: int | None = None,
) -> dict:
    """List regions visible to the active actor.

    Scope rules (mirrors ROLE_INVITES_TREE):
      org_admin       → every region (optionally filtered by `?program_id=`)
      program_manager → only regions whose `program_id` matches the inviter's
                        own program (any `?program_id=` query is honored
                        only if it matches the PM's own program)
      region_manager  → only the region pinned on the membership
      everyone else   → []
    """
    org_id, role, scoped_region_id = _org_ctx(membership)

    repo = RegionsRepository(db)
    rows = await repo.list_for_org(org_id)

    # region_manager only sees their own region.
    if role == "region_manager" and scoped_region_id is not None:
        rows = [r for r in rows if r.id == scoped_region_id]

    # Phase 12 — regions are shared geography. PM now sees EVERY region that
    # hosts at least one team from their program (rather than regions where
    # Region.program_id == their program, which is always NULL post-migration).
    if role == "program_manager":
        pm_program_id = getattr(membership, "program_id", None)
        if pm_program_id is None:
            return {"regions": []}
        active_rids_q = await db.execute(
            select(TeamProfile.region_id)
            .where(
                TeamProfile.organization_id == org_id,
                TeamProfile.program_id == pm_program_id,
                TeamProfile.region_id.is_not(None),
            )
            .distinct()
        )
        active_rids = {rid for (rid,) in active_rids_q.all()}
        rows = [r for r in rows if r.id in active_rids]
        program_id = pm_program_id  # surface in response context

    # org_admin honors the explicit ?program_id= filter — same axis as PM:
    # regions that host at least one team in that program.
    if role == "org_admin" and program_id is not None:
        active_rids_q = await db.execute(
            select(TeamProfile.region_id)
            .where(
                TeamProfile.organization_id == org_id,
                TeamProfile.program_id == program_id,
                TeamProfile.region_id.is_not(None),
            )
            .distinct()
        )
        active_rids = {rid for (rid,) in active_rids_q.all()}
        rows = [r for r in rows if r.id in active_rids]

    rows.sort(key=lambda r: r.name)

    enrichment = await _enrich_regions(db, org_id, [r.id for r in rows])
    out = []
    for r in rows:
        base = RegionOut.model_validate(r).model_dump(mode="json")
        base.update(enrichment.get(r.id, {}))
        out.append(base)
    return {"regions": out, "program_id_filter": program_id}


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

    # Validate program belongs to this org (404 — never confirms cross-org).
    if body.program_id is not None:
        prog = (await db.execute(
            select(Program).where(
                Program.id == body.program_id, Program.organization_id == org_id,
            )
        )).scalar_one_or_none()
        if prog is None:
            raise NotFoundError("Program not found")

    repo = RegionsRepository(db)
    if await repo.get_by_name(organization_id=org_id, name=name) is not None:
        raise ConflictError("A region with this name already exists.", code="duplicate_name")

    region = Region(organization_id=org_id, name=name, program_id=body.program_id)
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
        extra={"name": region.name, "program_id": region.program_id},
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
# GET /org/api/regions/{region_id}/detail — drill-down view (Phase 6)
# ---------------------------------------------------------------------------


@router.get("/{region_id}/detail", response_model=dict)
async def get_region_detail(
    region_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    """One-shot view for the per-region drill-down page.

    Returns: the region itself, its parent program, the teams scoped to it
    (both new `team.region_id` and legacy `branch.region_id` paths), the
    active coaches + region_manager, and today's practice schedule.

    PMs see this only for regions inside their own program (404 otherwise).
    """
    org_id = membership.organization_id
    region = await RegionsRepository(db).get_for_org(region_id, org_id)
    _ensure_region_visible(region, membership)
    # Phase 12 — PM visibility: region must host at least one team in their
    # program. The legacy region.program_id check is gone.
    pm_program_id_for_filter: int | None = None
    if membership.role == "program_manager":
        pm_program_id_for_filter = getattr(membership, "program_id", None)
        if pm_program_id_for_filter is None:
            raise NotFoundError("Region not found")
        has_team_in_region = (await db.execute(
            select(TeamProfile.id)
            .where(
                TeamProfile.organization_id == org_id,
                TeamProfile.region_id == region_id,
                TeamProfile.program_id == pm_program_id_for_filter,
            )
            .limit(1)
        )).scalar_one_or_none()
        if has_team_in_region is None:
            raise NotFoundError("Region not found")

    # Parent program — shared regions: derive from the teams that live here.
    #   PM viewer:           their own program (the slice they care about).
    #   exactly one program: that program.
    #   multiple programs:   None (no single "parent").
    program_payload: dict | None = None
    primary_program_id: int | None = pm_program_id_for_filter
    if primary_program_id is None:
        prog_ids_q = await db.execute(
            select(TeamProfile.program_id)
            .where(
                TeamProfile.organization_id == org_id,
                TeamProfile.region_id == region_id,
                TeamProfile.program_id.is_not(None),
            )
            .distinct()
        )
        distinct_progs = [pid for (pid,) in prog_ids_q.all()]
        if len(distinct_progs) == 1:
            primary_program_id = distinct_progs[0]
    if primary_program_id is not None:
        prog = await db.get(Program, primary_program_id)
        if prog is not None:
            program_payload = {"id": prog.id, "name": prog.name, "slug": prog.slug}

    # Teams in this region (dual path). For a PM viewer, narrow to their
    # program so the detail page doesn't accidentally surface cross-program
    # teams in the same shared region.
    team_filter = [
        TeamProfile.organization_id == org_id,
        or_(
            TeamProfile.region_id == region_id,
            Branch.region_id == region_id,
        ),
    ]
    if pm_program_id_for_filter is not None:
        team_filter.append(TeamProfile.program_id == pm_program_id_for_filter)
    team_rows = (await db.execute(
        select(TeamProfile, User.display_name, User.email)
        .outerjoin(User, User.id == TeamProfile.user_id)
        .outerjoin(Branch, Branch.id == TeamProfile.branch_id)
        .where(*team_filter)
        .order_by(TeamProfile.team_name)
    )).all()
    seen_team_ids: set[int] = set()
    teams_out: list[dict] = []
    for t, coach_name, coach_email in team_rows:
        if t.id in seen_team_ids:
            continue
        seen_team_ids.add(t.id)
        teams_out.append({
            "id": t.id,
            "team_name": t.team_name,
            "league": t.league,
            "division": t.division,
            "coach_user_id": t.user_id,
            "coach_display_name": coach_name or coach_email,
        })

    # Members (region_manager + coaches) attached to this region either
    # directly or via a branch within the region.
    member_rows = (await db.execute(
        select(UserOrganization, User)
        .join(User, User.id == UserOrganization.user_id)
        .outerjoin(Branch, Branch.id == UserOrganization.branch_id)
        .where(
            UserOrganization.organization_id == org_id,
            UserOrganization.status == "active",
            or_(
                UserOrganization.region_id == region_id,
                Branch.region_id == region_id,
            ),
        )
        .order_by(UserOrganization.role, User.display_name)
    )).all()
    seen_member_ids: set[int] = set()
    members_out: list[dict] = []
    for uo, user in member_rows:
        if uo.id in seen_member_ids:
            continue
        seen_member_ids.add(uo.id)
        members_out.append({
            "membership_id": uo.id,
            "user_id": user.id,
            "display_name": user.display_name or user.email,
            "email": user.email,
            "role": uo.role,
        })

    # Today's practices in this region (server local date).
    from datetime import date as _date
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta
    today = _date.today()
    start = _datetime.combine(today, _datetime.min.time())
    end = start + _timedelta(days=1)
    practice_rows = (await db.execute(
        select(PracticeSession, TeamProfile.team_name)
        .join(TeamProfile, TeamProfile.id == PracticeSession.team_id)
        .where(
            PracticeSession.region_id == region_id,
            PracticeSession.scheduled_at >= start,
            PracticeSession.scheduled_at < end,
        )
        .order_by(PracticeSession.scheduled_at)
    )).all()
    practices_out = [
        {
            "id": p.id,
            "team_id": p.team_id,
            "team_name": tname,
            "title": p.title,
            "scheduled_at": p.scheduled_at.isoformat() if p.scheduled_at else None,
            "duration_minutes": p.duration_minutes,
            "location": p.location,
            "status": p.status,
        }
        for (p, tname) in practice_rows
    ]

    return {
        "region": {
            "id": region.id,
            "name": region.name,
            "program_id": region.program_id,
        },
        "program": program_payload,
        "teams": teams_out,
        "members": members_out,
        "practices_today": practices_out,
        "today": today.isoformat(),
    }


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

    # program_id changes — None unsets, int sets (must belong to this org).
    if "program_id" in body.model_fields_set and body.program_id != region.program_id:
        if body.program_id is not None:
            prog = (await db.execute(
                select(Program).where(
                    Program.id == body.program_id,
                    Program.organization_id == membership.organization_id,
                )
            )).scalar_one_or_none()
            if prog is None:
                raise NotFoundError("Program not found")
        changes["program_id"] = {"from": region.program_id, "to": body.program_id}
        region.program_id = body.program_id

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
