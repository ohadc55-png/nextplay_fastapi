"""GET /org/api/programs — list programs in the active org.
POST /org/api/programs — create a new program (org_admin only).

Powers two surfaces:
  1. The Program dropdown in the Invite Member modal (always lean).
  2. The /org/programs rollup page (`?with_counts=true`), which adds per-
     program region/team/coach/player aggregates so the admin can see the
     whole hierarchy at a glance without drilling down.

Scoped per role:
  - org_admin       -> all programs in the org   (+ can create new ones)
  - program_manager -> only their own program
  - region_manager  -> the parent program of their region (single item)
  - others          -> []
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership
from src.core.database import get_db
from src.core.exceptions import ConflictError, ForbiddenError, ValidationError
from src.models.players import Player
from src.models.programs import Program
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.repositories.programs_repo import ProgramsRepository
from src.services.org_audit_service import log_org_action


# Roles allowed to create programs. Currently just org_admin per
# product owner; widen here when other Enterprise-management roles
# land. Centralized so future CRUD endpoints (PATCH/DELETE) can
# import the same set without drift.
PROGRAM_WRITE_ROLES = frozenset({"org_admin"})

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/programs", tags=["org-programs"])


def _serialize(p: Program) -> dict:
    return {
        "id": p.id,
        "organization_id": p.organization_id,
        "name": p.name,
        "slug": p.slug,
    }


async def _enrich_programs(
    db: AsyncSession, org_id: int, program_ids: list[int],
) -> dict[int, dict]:
    """Per-program rollup: regions, teams, coaches, active players.

    Teams + players are counted through BOTH the new direct `team.region_id`
    path and the legacy `team.branch_id -> branch.region_id` path. The
    aggregation pivots on the program by going through Region. Five queries.
    """
    if not program_ids:
        return {}

    # Phase 12 — rollup now pivots on TeamProfile.program_id directly. The
    # legacy Region.program_id mapping is gone (regions are shared geography),
    # so "regions per program" = distinct regions where that program has teams.

    # 1) teams per program — primary axis, drives most of the downstream counts.
    team_rows = (await db.execute(
        select(TeamProfile.program_id, TeamProfile.id, TeamProfile.region_id)
        .where(
            TeamProfile.organization_id == org_id,
            TeamProfile.program_id.in_(program_ids),
        )
    )).all()
    teams_by_program: dict[int, set[int]] = {}
    regions_by_program: dict[int, set[int]] = {}
    for pid, tid, rid in team_rows:
        teams_by_program.setdefault(pid, set()).add(tid)
        if rid is not None:
            regions_by_program.setdefault(pid, set()).add(rid)
    team_count = {pid: len(s) for pid, s in teams_by_program.items()}
    region_count = {pid: len(s) for pid, s in regions_by_program.items()}

    # 2) coaches per program — UserOrganization.program_id only. With regions
    #    shared across programs, a region_manager isn't a "coach of program X"
    #    by virtue of geography; we only count coaches explicitly pinned.
    coach_rows: dict[int, set[int]] = {}
    direct_coach = (await db.execute(
        select(UserOrganization.program_id, UserOrganization.id)
        .where(
            UserOrganization.organization_id == org_id,
            UserOrganization.role == "coach",
            UserOrganization.status == "active",
            UserOrganization.program_id.in_(program_ids),
        )
    )).all()
    for pid, uoid in direct_coach:
        coach_rows.setdefault(pid, set()).add(uoid)
    coach_count = {pid: len(s) for pid, s in coach_rows.items()}

    # 4) active players per program — players whose team is in any region of
    #    the program (dual path resolved via teams_by_program above).
    player_count: dict[int, int] = {}
    if any(teams_by_program.values()):
        all_team_ids = {tid for s in teams_by_program.values() for tid in s}
        team_to_program: dict[int, int] = {}
        for pid, s in teams_by_program.items():
            for tid in s:
                team_to_program[tid] = pid
        player_rows = (await db.execute(
            select(Player.team_id, func.count(Player.id))
            .where(
                Player.organization_id == org_id,
                Player.active.is_(True),
                Player.team_id.in_(all_team_ids),
            )
            .group_by(Player.team_id)
        )).all()
        for tid, c in player_rows:
            pid = team_to_program.get(tid)
            if pid is not None:
                player_count[pid] = player_count.get(pid, 0) + int(c)

    # 5) program_manager name per program (first by id for determinism).
    from src.models.users import User
    mgr_rows = (await db.execute(
        select(
            UserOrganization.program_id,
            User.display_name,
            User.email,
            UserOrganization.id,
        )
        .join(User, User.id == UserOrganization.user_id)
        .where(
            UserOrganization.organization_id == org_id,
            UserOrganization.role == "program_manager",
            UserOrganization.status == "active",
            UserOrganization.program_id.in_(program_ids),
        )
        .order_by(UserOrganization.id)
    )).all()
    manager_name: dict[int, str] = {}
    for pid, dname, email, _uo in mgr_rows:
        if pid not in manager_name:
            manager_name[pid] = dname or email

    return {
        pid: {
            "region_count": region_count.get(pid, 0),
            "team_count": team_count.get(pid, 0),
            "coach_count": coach_count.get(pid, 0),
            "player_count": player_count.get(pid, 0),
            "manager_name": manager_name.get(pid),
        }
        for pid in program_ids
    }


@router.get("", response_model=dict)
async def list_programs(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
    with_counts: bool = Query(
        default=False,
        description="Include per-program rollup counts (regions/teams/coaches/players).",
    ),
) -> dict:
    org_id = membership.organization_id
    role = membership.role

    if role == "program_manager":
        pm_program_id = getattr(membership, "program_id", None)
        if pm_program_id is None:
            return {"programs": []}
        prog = await db.get(Program, pm_program_id)
        rows = [prog] if prog else []
    elif role == "region_manager" and membership.region_id is not None:
        # Phase 12 — a region now hosts teams from many programs, so the RM
        # sees every program in the org (programs they touch via their region).
        # Filtering to only the programs that actually have teams in their
        # region keeps the list relevant without leaking org-wide enumeration.
        active_pids_q = await db.execute(
            select(TeamProfile.program_id)
            .where(
                TeamProfile.organization_id == org_id,
                TeamProfile.region_id == membership.region_id,
                TeamProfile.program_id.is_not(None),
            )
            .distinct()
        )
        active_pids = {pid for (pid,) in active_pids_q.all()}
        if not active_pids:
            return {"programs": []}
        rows = [
            p for p in await ProgramsRepository(db).list_for_org(org_id)
            if p.id in active_pids
        ]
        rows.sort(key=lambda p: p.name)
    elif role != "org_admin":
        # coach, viewer, branch_manager (legacy) → no need to enumerate programs.
        return {"programs": []}
    else:
        rows = await ProgramsRepository(db).list_for_org(org_id)
        rows.sort(key=lambda p: p.name)

    if not with_counts:
        return {"programs": [_serialize(p) for p in rows]}

    enrichment = await _enrich_programs(db, org_id, [p.id for p in rows])
    out: list[dict] = []
    for p in rows:
        base = _serialize(p)
        base.update(enrichment.get(p.id, {}))
        out.append(base)
    return {"programs": out}


class ProgramCreateRequest(BaseModel):
    """Body for POST /org/api/programs.

    `slug` is optional — if omitted we derive it from name. Stored as
    lower-kebab-case (so URLs stay clean if/when programs ever appear
    in tenant paths)."""

    name: str = Field(min_length=1, max_length=100)
    slug: str | None = Field(default=None, max_length=64)


_SLUG_VALID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SLUG_NORMALIZE_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _derive_slug(name: str) -> str:
    """Turn a free-form name (Hebrew/English/punct) into a URL-safe slug.

    Hebrew names won't survive the ascii filter — they fall back to a
    timestamp-anchored placeholder so the row is still creatable. The
    admin can edit the slug later from the dashboard once that UI exists.
    """
    lowered = name.strip().lower()
    cleaned = _SLUG_NORMALIZE_NON_ALNUM.sub("-", lowered).strip("-")
    if not cleaned or not _SLUG_VALID.match(cleaned):
        # Hebrew-only or all-punctuation names → safe fallback.
        import secrets
        cleaned = f"program-{secrets.token_hex(3)}"
    return cleaned[:64]


@router.post("", response_model=dict, status_code=201)
async def create_program(
    body: ProgramCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    """Create a new program inside the caller's active org. Restricted to
    roles in `PROGRAM_WRITE_ROLES`. Slug is auto-derived from name if
    omitted. Audit log captures the actor + the resulting program."""
    if membership.role not in PROGRAM_WRITE_ROLES:
        # Match the existing 403 surface; the caller is authenticated
        # but their role bars writes. (Org guard in get_current_org_membership
        # already prevents cross-org access.)
        raise ForbiddenError("Only organization admins can create programs.")

    org_id = membership.organization_id
    repo = ProgramsRepository(db)

    name = body.name.strip()
    if not name:
        raise ValidationError("Program name is required.", code="name_required")

    slug = (body.slug or "").strip().lower() or _derive_slug(name)
    if not _SLUG_VALID.match(slug):
        raise ValidationError(
            "Slug must be lowercase letters, digits, and hyphens only.",
            code="invalid_slug",
        )

    # Uniqueness: name is hard (DB constraint); slug is soft (we enforce
    # in code to surface a clean error instead of a 500). Both bound to
    # the active org — names can repeat across orgs.
    if await repo.get_by_name(organization_id=org_id, name=name) is not None:
        raise ConflictError("A program with that name already exists.", code="name_taken")
    if await repo.get_by_slug(organization_id=org_id, slug=slug) is not None:
        raise ConflictError("A program with that slug already exists.", code="slug_taken")

    program = await repo.create(organization_id=org_id, name=name, slug=slug)

    await log_org_action(
        db,
        organization_id=org_id,
        actor_user_id=membership.user_id,
        actor_email=None,
        action="program.create",
        target_type="program",
        target_id=program.id,
        request=request,
        extra={"name": name, "slug": slug},
    )

    return {"program": _serialize(program)}


__all__ = ["router"]
