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
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.models.branches import Branch
from src.models.players import Player
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
    404 covers cross-org, out-of-region, out-of-branch, and (for coach role)
    not-the-owner cases."""
    team = await TeamsRepository(db).get_for_org(team_id, membership.organization_id)
    if team is None:
        raise NotFoundError("Team not found")

    role = membership.role
    if role == "region_manager":
        # Team's branch must live in actor's region.
        if team.branch_id is None:
            raise NotFoundError("Team not found")
        branch = await db.get(Branch, team.branch_id)
        if branch is None or branch.region_id != membership.region_id:
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
    branch_id: int | None = Query(default=None),
    region_id: int | None = Query(default=None),
) -> dict:
    repo = TeamsRepository(db)
    role = membership.role

    if role == "coach":
        rows = await repo.list_for_org(
            membership.organization_id, coach_user_id=membership.user_id
        )
    elif role == "branch_manager":
        rows = await repo.list_for_org(
            membership.organization_id, branch_id=membership.branch_id
        )
    elif role == "region_manager":
        rows = await repo.list_for_org(
            membership.organization_id, region_id=membership.region_id
        )
    else:  # org_admin / viewer
        rows = await repo.list_for_org(
            membership.organization_id, region_id=region_id, branch_id=branch_id
        )
    return {
        "teams": [
            OrgTeamOut.model_validate(t).model_dump(mode="json") for t in rows
        ]
    }


# ---------------------------------------------------------------------------
# POST /org/api/teams — create
# ---------------------------------------------------------------------------


@router.post("", response_model=OrgTeamOut, status_code=status.HTTP_201_CREATED)
async def create_team(
    body: OrgTeamCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager", "branch_manager")
    ),
) -> OrgTeamOut:
    org_id = membership.organization_id
    target_branch_id = await _validate_branch_in_scope(
        db, org_id=org_id, branch_id=body.branch_id, membership=membership,
    )

    team = TeamProfile(
        organization_id=org_id,
        branch_id=target_branch_id,
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
    return OrgTeamOut.model_validate(team)


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
        require_role("org_admin", "region_manager", "branch_manager")
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
        require_role("org_admin", "region_manager", "branch_manager")
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
        require_role("org_admin", "region_manager", "branch_manager")
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
