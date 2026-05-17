"""Team Management under /org/api/teams/* (Phase 1.5).

The org-context API for the same `team_profile` table the Coach App reads
through `/api/teams/*`. Critical invariant: every read here filters by
`organization_id`, so private-coach teams (`organization_id IS NULL`) never
leak in. Conversely, /api/teams/* (Phase 0) is unchanged — private coaches
remain unaffected.

Single-owner model: `team_profile.user_id` IS the head coach. Multi-coach
support is deferred to a post-Phase-1 sub-phase.

Auth & roles (org session via `get_current_org_membership`):

- GET    /org/api/teams                    any active member (scoped per role)
- POST   /org/api/teams                    org_admin / region_manager / branch_manager
- GET    /org/api/teams/{id}               scoped read
- PATCH  /org/api/teams/{id}               scoped write
- DELETE /org/api/teams/{id}               org_admin (refuses if players still attached)
- POST   /org/api/teams/{id}/coaches       org_admin / region_manager / branch_manager
                                           (idempotent: same user → no-op)
- DELETE /org/api/teams/{id}/coaches/{uid} org_admin / region_manager / branch_manager

Cross-org or role-mismatch hits → 404 (never 403).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership, require_role
from src.core.database import get_db
from src.core.exceptions import ConflictError, NotFoundError
from src.models.branches import Branch
from src.models.players import Player
from src.models.programs import Program
from src.models.regions import Region
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.models.users import User
from src.repositories.teams_repo import TeamsRepository
from src.repositories.user_organizations_repo import UserOrganizationsRepository
from src.repositories.users_repo import UsersRepository
from src.schemas.org_teams import (
    CoachAssignRequest,
    OrgTeamCreate,
    OrgTeamOut,
    OrgTeamUpdate,
)
from src.services.org_audit_service import log_org_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/teams", tags=["org-teams"])


# ---------------------------------------------------------------------------
# Helpers — scope + visibility
# ---------------------------------------------------------------------------


async def _validate_region_in_scope(
    db: AsyncSession,
    *,
    org_id: int,
    region_id: int | None,
    membership: UserOrganization,
) -> int | None:
    """Validate `region_id` against the org + the actor's scope.

    - region_id=None is allowed (team has no region assigned).
    - Any region_id outside org_id → NotFoundError(404).
    - region_manager: region MUST equal membership.region_id.
    - program_manager: region MUST belong to membership.program_id.
    """
    if region_id is None:
        return None
    region = (
        await db.execute(
            select(Region).where(
                Region.id == region_id, Region.organization_id == org_id
            )
        )
    ).scalar_one_or_none()
    if region is None:
        raise NotFoundError("Region not found")
    if membership.role == "region_manager":
        if region.id != membership.region_id:
            raise NotFoundError("Region not found")
    # Phase 12 — PM no longer scopes by region. Regions are shared geography
    # across all programs; a PM can create teams in any region. The team's
    # program is set explicitly via TeamProfile.program_id at create time.
    return region.id


async def _validate_program_in_scope(
    db: AsyncSession,
    *,
    org_id: int,
    program_id: int | None,
    membership: UserOrganization,
) -> int | None:
    """Resolve the final program_id stored on a created/updated team.

    Clamp rules:
      - program_manager: ALWAYS forced to their own program_id; any input
        that mismatches → NotFoundError (404 cloak, not 403).
      - org_admin / region_manager / branch_manager: input is accepted if
        the program belongs to the org; None is allowed.
      - PM with no program_id on their membership → NotFoundError.
    """
    pm_program = (
        getattr(membership, "program_id", None)
        if membership.role == "program_manager"
        else None
    )
    if membership.role == "program_manager":
        if pm_program is None:
            raise NotFoundError("Program not found")
        if program_id is not None and program_id != pm_program:
            raise NotFoundError("Program not found")
        return pm_program

    if program_id is None:
        return None
    program = (
        await db.execute(
            select(Program).where(
                Program.id == program_id, Program.organization_id == org_id
            )
        )
    ).scalar_one_or_none()
    if program is None:
        raise NotFoundError("Program not found")
    return program.id


async def _validate_branch_in_scope(
    db: AsyncSession,
    *,
    org_id: int,
    branch_id: int | None,
    membership: UserOrganization,
) -> int | None:
    """Validate `branch_id` against the org + the actor's scope.

    - branch_id=None is allowed for org_admin (unassigned team).
    - region_manager + branch_id=None: forced to a branch is NOT required
      (they can also pin to any branch within their region).
    - branch_manager + branch_id=None: forced to their own branch.
    - Any branch_id that doesn't belong to org_id, or that's outside the
      actor's region/branch scope → NotFoundError(404).
    """
    if branch_id is None:
        # Branch managers must scope every team to their branch.
        if membership.role == "branch_manager" and membership.branch_id is not None:
            return membership.branch_id
        return None

    branch = (
        await db.execute(
            select(Branch).where(
                Branch.id == branch_id, Branch.organization_id == org_id
            )
        )
    ).scalar_one_or_none()
    if branch is None:
        raise NotFoundError("Branch not found")

    if membership.role == "region_manager":
        if branch.region_id != membership.region_id:
            raise NotFoundError("Branch not found")
    elif membership.role == "branch_manager":
        if branch.id != membership.branch_id:
            raise NotFoundError("Branch not found")
    return branch.id


async def _ensure_team_visible(
    db: AsyncSession, team_id: int, membership: UserOrganization,
) -> TeamProfile:
    """Fetch a team within the org and verify the actor can see it.

    404 covers cross-org, out-of-program (for PM), out-of-region (for RM),
    out-of-branch (for BM), and not-the-owner (for coach) cases.

    Visibility honors BOTH the new direct `team.region_id` FK and the
    legacy `team.branch_id -> branch.region_id` path.
    """
    team = await TeamsRepository(db).get_for_org(team_id, membership.organization_id)
    if team is None:
        raise NotFoundError("Team not found")

    role = membership.role
    if role == "program_manager":
        pm_program = getattr(membership, "program_id", None)
        if pm_program is None:
            raise NotFoundError("Team not found")
        # Phase 12 — team's program is on TeamProfile directly. The legacy
        # walk through region.program_id is gone (regions are shared).
        if team.program_id != pm_program:
            raise NotFoundError("Team not found")
    elif role == "region_manager":
        # Phase 12 — when RM is pinned to a program, also enforce
        # team.program_id == rm.program_id. Otherwise region scope alone.
        rm_program = getattr(membership, "program_id", None)
        if rm_program is not None and team.program_id != rm_program:
            raise NotFoundError("Team not found")
        # Direct OR legacy branch path.
        eff_region_id = team.region_id
        if eff_region_id is None and team.branch_id is not None:
            branch = await db.get(Branch, team.branch_id)
            eff_region_id = branch.region_id if branch else None
        if eff_region_id != membership.region_id:
            raise NotFoundError("Team not found")
    elif role == "branch_manager":
        if team.branch_id != membership.branch_id:
            raise NotFoundError("Team not found")
    elif role == "coach":
        if team.user_id != membership.user_id:
            raise NotFoundError("Team not found")
    return team


# ---------------------------------------------------------------------------
# GET /org/api/teams — list
# ---------------------------------------------------------------------------


@router.get("", response_model=dict)
async def list_teams(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
    program_id: int | None = Query(default=None),
    region_id: int | None = Query(default=None),
    branch_id: int | None = Query(default=None),
    coach_user_id: int | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
) -> dict:
    """List teams visible to the active actor, with optional filters.

    Filters (org_admin only — non-admin roles get auto-scoped):
      ?program_id, ?region_id, ?branch_id, ?coach_user_id, ?q (name search)

    Scope clamps (mirror ROLE_INVITES_TREE):
      program_manager → forced to their own program (any program_id query
                        outside that is ignored — 404 cloak via empty result)
      region_manager  → forced to their own region
      branch_manager  → forced to their own branch
      coach           → forced to teams they own (user_id match)

    Response includes per-team enrichment: region_name, program_id,
    program_name, coach_display_name, coach_email.
    """
    repo = TeamsRepository(db)
    role = membership.role
    org_id = membership.organization_id

    # Per-role scope clamps. Non-admin filter values that don't match the
    # actor's pinned scope yield an empty list (404 cloak — never confirms
    # that an out-of-scope id even exists).
    if role == "program_manager":
        pm_program = getattr(membership, "program_id", None)
        if pm_program is None or (program_id is not None and program_id != pm_program):
            return {"teams": [], "filters": _filter_echo(
                program_id, region_id, branch_id, coach_user_id, q,
            )}
        program_id = pm_program
    elif role == "region_manager":
        rm_region = membership.region_id
        if rm_region is None or (region_id is not None and region_id != rm_region):
            return {"teams": [], "filters": _filter_echo(
                program_id, region_id, branch_id, coach_user_id, q,
            )}
        region_id = rm_region
        # Phase 12 — when the RM membership pins them to a single program,
        # also clamp by program_id so they see only their (region × program)
        # slice. Inputs that try to widen this are 404-cloaked.
        rm_program = getattr(membership, "program_id", None)
        if rm_program is not None:
            if program_id is not None and program_id != rm_program:
                return {"teams": [], "filters": _filter_echo(
                    program_id, region_id, branch_id, coach_user_id, q,
                )}
            program_id = rm_program
    elif role == "branch_manager":
        bm_branch = membership.branch_id
        if bm_branch is None or (branch_id is not None and branch_id != bm_branch):
            return {"teams": [], "filters": _filter_echo(
                program_id, region_id, branch_id, coach_user_id, q,
            )}
        branch_id = bm_branch
    elif role == "coach":
        # Coaches only see their own teams regardless of any filter.
        coach_user_id = membership.user_id

    rows = await repo.list_for_org(
        org_id,
        program_id=program_id,
        region_id=region_id,
        branch_id=branch_id,
        coach_user_id=coach_user_id,
        q=q,
    )
    enrichment = await _enrich_teams(db, rows)
    out: list[dict] = []
    for t in rows:
        base = OrgTeamOut.model_validate(t).model_dump(mode="json")
        base.update(enrichment.get(t.id, {}))
        out.append(base)
    return {
        "teams": out,
        "filters": _filter_echo(program_id, region_id, branch_id, coach_user_id, q),
    }


def _filter_echo(
    program_id, region_id, branch_id, coach_user_id, q,
) -> dict:
    return {
        "program_id": program_id,
        "region_id": region_id,
        "branch_id": branch_id,
        "coach_user_id": coach_user_id,
        "q": q,
    }


async def _enrich_teams(
    db: AsyncSession, teams: list[TeamProfile],
) -> dict[int, dict]:
    """One-shot batch lookup of derived display fields per team.

    For each team we want: region_name (from team.region_id or
    branch.region_id), program_id + program_name (from the resolved region),
    coach_display_name + coach_email (from team.user_id → users).

    All four lookups happen via IN-list selects keyed off the input team
    set — no N+1.
    """
    if not teams:
        return {}

    # Effective region id per team: prefer direct team.region_id, else
    # branch.region_id, else None.
    branch_ids = {t.branch_id for t in teams if t.branch_id is not None}
    branch_to_region: dict[int, int | None] = {}
    if branch_ids:
        rows = (await db.execute(
            select(Branch.id, Branch.region_id).where(Branch.id.in_(branch_ids))
        )).all()
        branch_to_region = dict(rows)

    effective_region: dict[int, int | None] = {}
    for t in teams:
        rid = t.region_id
        if rid is None and t.branch_id is not None:
            rid = branch_to_region.get(t.branch_id)
        effective_region[t.id] = rid

    region_ids = {rid for rid in effective_region.values() if rid is not None}
    region_names: dict[int, str | None] = {}
    if region_ids:
        rows = (await db.execute(
            select(Region.id, Region.name).where(Region.id.in_(region_ids))
        )).all()
        region_names = dict(rows)

    # Phase 12 — program is now a direct attribute on the team, not derived
    # from the region. Build a {program_id → program_name} lookup off the
    # set of program_ids the teams actually point at.
    program_ids = {t.program_id for t in teams if t.program_id is not None}
    program_names: dict[int, str | None] = {}
    if program_ids:
        rows = (await db.execute(
            select(Program.id, Program.name).where(Program.id.in_(program_ids))
        )).all()
        program_names = dict(rows)

    user_ids = {t.user_id for t in teams if t.user_id is not None}
    coach_info: dict[int, tuple[str | None, str | None]] = {}
    if user_ids:
        rows = (await db.execute(
            select(User.id, User.display_name, User.email).where(User.id.in_(user_ids))
        )).all()
        for uid, dname, email in rows:
            coach_info[uid] = (dname or email, email)

    out: dict[int, dict] = {}
    for t in teams:
        rid = effective_region.get(t.id)
        rname = region_names.get(rid) if rid is not None else None
        pid = t.program_id
        pname = program_names.get(pid) if pid is not None else None
        coach_name, coach_email = (None, None)
        if t.user_id is not None and t.user_id in coach_info:
            coach_name, coach_email = coach_info[t.user_id]
        out[t.id] = {
            "region_name": rname,
            "program_id": pid,
            "program_name": pname,
            "coach_display_name": coach_name,
            "coach_email": coach_email,
        }
    return out


# ---------------------------------------------------------------------------
# POST /org/api/teams — create
# ---------------------------------------------------------------------------


@router.post("", response_model=OrgTeamOut, status_code=status.HTTP_201_CREATED)
async def create_team(
    body: OrgTeamCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "program_manager", "region_manager", "branch_manager")
    ),
) -> OrgTeamOut:
    org_id = membership.organization_id
    target_branch_id = await _validate_branch_in_scope(
        db, org_id=org_id, branch_id=body.branch_id, membership=membership,
    )
    target_region_id = await _validate_region_in_scope(
        db, org_id=org_id, region_id=body.region_id, membership=membership,
    )
    # Phase 12 — program_id is independent of region. PM is auto-clamped to
    # their own program; org_admin can pick any program in the org; RM/BM
    # may pass it through or leave it NULL (their region/branch holds
    # teams from multiple programs and the form may not surface it).
    target_program_id = await _validate_program_in_scope(
        db, org_id=org_id, program_id=body.program_id, membership=membership,
    )

    team = TeamProfile(
        organization_id=org_id,
        branch_id=target_branch_id,
        region_id=target_region_id,
        program_id=target_program_id,
        team_name=body.team_name.strip(),
        league=body.league,
        division=body.division,
        play_style=body.play_style,
        notes=body.notes,
    )
    db.add(team)
    await db.flush()
    await db.refresh(team)

    await log_org_action(
        db,
        organization_id=org_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="team.create",
        target_type="team",
        target_id=team.id,
        request=request,
        extra={"team_name": team.team_name, "branch_id": team.branch_id},
    )
    return OrgTeamOut.model_validate(team)


# ---------------------------------------------------------------------------
# GET /org/api/teams/{team_id} — detail
# ---------------------------------------------------------------------------


@router.get("/{team_id}", response_model=OrgTeamOut)
async def get_team(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> OrgTeamOut:
    team = await _ensure_team_visible(db, team_id, membership)
    enrichment = await _enrich_teams(db, [team])
    base = OrgTeamOut.model_validate(team).model_dump(mode="json")
    base.update(enrichment.get(team.id, {}))
    return OrgTeamOut.model_validate(base)


# ---------------------------------------------------------------------------
# GET /org/api/teams/{team_id}/detail — drill-down view (Phase 8)
# ---------------------------------------------------------------------------


@router.get("/{team_id}/detail", response_model=dict)
async def get_team_detail(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    """One-shot detail payload for the team drill-down page.

    Returns the team (with region + program + coach enriched), the active
    roster (players where active=True), and today's practice sessions for
    this team. 404 covers any cross-scope access via `_ensure_team_visible`.
    """
    from datetime import date as _date
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    from src.models.practice_sessions import PracticeSession
    from src.repositories.players_repo import PlayersRepository

    team = await _ensure_team_visible(db, team_id, membership)
    enrichment = await _enrich_teams(db, [team])
    base = OrgTeamOut.model_validate(team).model_dump(mode="json")
    base.update(enrichment.get(team.id, {}))

    # Active roster — org-scoped to defend against the unlikely edge case
    # of a team_id collision across orgs.
    players_repo = PlayersRepository(db)
    players = await players_repo.list_for_org(
        team.organization_id, team_id=team.id, include_inactive=False,
    )
    roster = [
        {
            "id": p.id,
            "name": p.name,
            "number": p.number,
            "position": p.position,
            "age": p.age,
            "dominant_hand": p.dominant_hand,
            "active": bool(p.active) if p.active is not None else True,
        }
        for p in players
    ]

    # Today's practice for this team
    today = _date.today()
    start = _datetime.combine(today, _datetime.min.time())
    end = start + _timedelta(days=1)
    practice_rows = (await db.execute(
        select(PracticeSession)
        .where(
            PracticeSession.team_id == team.id,
            PracticeSession.scheduled_at >= start,
            PracticeSession.scheduled_at < end,
        )
        .order_by(PracticeSession.scheduled_at)
    )).scalars().all()
    practices_today = [
        {
            "id": p.id,
            "title": p.title,
            "scheduled_at": p.scheduled_at.isoformat() if p.scheduled_at else None,
            "duration_minutes": p.duration_minutes,
            "location": p.location,
            "status": p.status,
        }
        for p in practice_rows
    ]

    return {
        "team": base,
        "roster": roster,
        "practices_today": practices_today,
        "today": today.isoformat(),
    }


# ---------------------------------------------------------------------------
# PATCH /org/api/teams/{team_id} — update
# ---------------------------------------------------------------------------


@router.patch("/{team_id}", response_model=OrgTeamOut)
async def update_team(
    team_id: int,
    body: OrgTeamUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "program_manager", "region_manager", "branch_manager")
    ),
) -> OrgTeamOut:
    team = await _ensure_team_visible(db, team_id, membership)

    changes: dict = {}
    sentinel = object()

    def _set(attr: str, new):
        old = getattr(team, attr)
        if new != old:
            changes[attr] = {"from": old, "to": new}
            setattr(team, attr, new)

    if body.team_name is not None:
        _set("team_name", body.team_name.strip())
    if body.league is not None:
        _set("league", body.league)
    if body.division is not None:
        _set("division", body.division)
    if body.play_style is not None:
        _set("play_style", body.play_style)
    if body.strengths is not None:
        _set("strengths", body.strengths)
    if body.weaknesses is not None:
        _set("weaknesses", body.weaknesses)
    if body.notes is not None:
        _set("notes", body.notes)

    if "branch_id" in body.model_fields_set:
        target_branch_id = await _validate_branch_in_scope(
            db, org_id=membership.organization_id,
            branch_id=body.branch_id, membership=membership,
        )
        _set("branch_id", target_branch_id)

    if "region_id" in body.model_fields_set:
        target_region_id = await _validate_region_in_scope(
            db, org_id=membership.organization_id,
            region_id=body.region_id, membership=membership,
        )
        _set("region_id", target_region_id)

    if "program_id" in body.model_fields_set:
        # PM is clamped server-side; if their input doesn't match their own
        # program the validator raises 404. org_admin can move teams between
        # programs freely.
        target_program_id = await _validate_program_in_scope(
            db, org_id=membership.organization_id,
            program_id=body.program_id, membership=membership,
        )
        _set("program_id", target_program_id)

    if changes:
        await db.flush()
        await db.refresh(team)
        await log_org_action(
            db,
            organization_id=membership.organization_id,
            actor_user_id=request.state.user.id,
            actor_email=request.state.user.email,
            action="team.update",
            target_type="team",
            target_id=team.id,
            request=request,
            extra=changes,
        )
    return OrgTeamOut.model_validate(team)


# ---------------------------------------------------------------------------
# DELETE /org/api/teams/{team_id} — delete (org_admin only)
# ---------------------------------------------------------------------------


@router.delete("/{team_id}", response_model=dict)
async def delete_team(
    team_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(require_role("org_admin")),
) -> dict:
    team = await TeamsRepository(db).get_for_org(team_id, membership.organization_id)
    if team is None:
        raise NotFoundError("Team not found")

    player_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Player)
                .where(Player.team_id == team.id)
            )
        ).scalar()
        or 0
    )
    if player_count > 0:
        raise ConflictError(
            f"Team has {player_count} player(s) attached. "
            "Move or delete them first.",
            code="team_has_players",
        )

    snapshot = {"team_name": team.team_name, "branch_id": team.branch_id}
    await db.delete(team)
    await db.flush()

    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="team.delete",
        target_type="team",
        target_id=team_id,
        request=request,
        extra=snapshot,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /org/api/teams/{team_id}/coaches — assign coach (idempotent)
# ---------------------------------------------------------------------------


@router.post("/{team_id}/coaches", response_model=OrgTeamOut, status_code=status.HTTP_200_OK)
async def assign_coach(
    team_id: int,
    body: CoachAssignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "program_manager", "region_manager", "branch_manager")
    ),
) -> OrgTeamOut:
    """Set `team.user_id` to the given user, and ensure that user has an
    active `UserOrganization(role='coach', branch_id=team.branch_id)` row.
    Idempotent: assigning the same user again is a no-op.
    """
    team = await _ensure_team_visible(db, team_id, membership)
    target_user = await UsersRepository(db).get_active(body.user_id)
    if target_user is None:
        raise NotFoundError("User not found")

    if team.user_id == target_user.id:
        return OrgTeamOut.model_validate(team)

    # Upsert the coach membership.
    existing = await UserOrganizationsRepository(db).get_active(
        user_id=target_user.id,
        organization_id=membership.organization_id,
        role="coach",
    )
    if existing is None:
        new_membership = UserOrganization(
            user_id=target_user.id,
            organization_id=membership.organization_id,
            role="coach",
            branch_id=team.branch_id,
            status="active",
            invited_by=request.state.user.id,
        )
        db.add(new_membership)
        await db.flush()
    else:
        # Keep the existing membership; if branch_id drifted, align it to
        # the team's branch (best-effort, doesn't change role).
        if existing.branch_id != team.branch_id:
            existing.branch_id = team.branch_id

    previous_owner = team.user_id
    team.user_id = target_user.id
    await db.flush()
    await db.refresh(team)

    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="team.coach_assign",
        target_type="team",
        target_id=team.id,
        request=request,
        extra={
            "team_id": team.id,
            "from_user_id": previous_owner,
            "to_user_id": target_user.id,
        },
    )
    return OrgTeamOut.model_validate(team)


# ---------------------------------------------------------------------------
# DELETE /org/api/teams/{team_id}/coaches/{user_id} — unassign
# ---------------------------------------------------------------------------


@router.delete("/{team_id}/coaches/{user_id}", response_model=dict)
async def unassign_coach(
    team_id: int,
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "program_manager", "region_manager", "branch_manager")
    ),
) -> dict:
    """Clear `team.user_id` if it matches `user_id`. Leaves the user's
    `UserOrganization(role='coach')` membership untouched — they're still
    a coach in the org, just not the head coach of this team."""
    team = await _ensure_team_visible(db, team_id, membership)
    if team.user_id != user_id:
        raise NotFoundError("Coach assignment not found")

    team.user_id = None
    await db.flush()

    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="team.coach_unassign",
        target_type="team",
        target_id=team.id,
        request=request,
        extra={"team_id": team.id, "user_id": user_id},
    )
    return {"ok": True}


__all__ = ["router"]
