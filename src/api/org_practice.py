"""Practice-schedule endpoints under /org/api/practice/* (Phase 3 + 12).

Phase 3 delivered:
  - GET /today                       sidebar quick-look

Phase 12 (calendar) adds:
  - GET    /range?start=&end=        list practices in a date range
  - POST   /                         create one (or many — recurring weekly)
  - PATCH  /{id}                     update a single occurrence
  - DELETE /{id}?series=true|false   delete one occurrence (or whole future series)

Write authorization mirrors ROLE_INVITES_TREE:
  org_admin       -> any team in the org
  program_manager -> teams in their program (team.region.program_id matches)
  region_manager  -> teams in their region (direct or via branch)
  branch_manager  -> teams in their branch (legacy)
  coach           -> READ-ONLY (no create / no edit / no delete)

Recurring sessions are stored as N independent rows sharing a `series_id`
in `attributes_json` — that way every fetch is a single range query, and
editing one occurrence doesn't ripple to the others by accident. Deleting
"the series" is supported via the explicit `?series=true` query.
"""

from __future__ import annotations

import logging
import secrets
from datetime import date as date_type
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership
from src.core.database import get_db
from src.core.exceptions import NotFoundError, ValidationError
from src.models.branches import Branch
from src.models.practice_sessions import PracticeSession
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.repositories.practice_sessions_repo import PracticeSessionsRepository
from src.services.org_audit_service import log_org_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/practice", tags=["org-practice"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


_VALID_KINDS: frozenset[str] = frozenset({"practice", "game", "meeting", "other"})


class PracticeCreate(BaseModel):
    """Body for POST /org/api/practice.

    Two shapes:
      - Single team: pass `team_id`.
      - Bulk teams: pass `team_ids=[id1, id2, …]` (typically the IDs the
        frontend has cached from the current cascade filter when the user
        picks "all teams").
    Exactly one of the two MUST be provided; passing both rejects.

    `recurrence` is optional. When set to `"weekly"`, the server creates N
    occurrences (one per `recurrence_until - scheduled_at` week boundary)
    that share a generated `series_id` stamped in `attributes_json`. With
    `team_ids`, total rows = teams × occurrences (cross-product).

    `kind` distinguishes a regular practice from a game / meeting / other
    event. Stored alongside `series_id` in `attributes_json`. Defaults to
    "practice" to keep the legacy single-button flow working.
    """

    team_id: int | None = None
    team_ids: list[int] | None = Field(
        default=None,
        description="Bulk variant — one row per id is created.",
    )
    title: str | None = Field(default=None, max_length=200)
    scheduled_at: datetime
    duration_minutes: int | None = Field(default=None, ge=0, le=600)
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    kind: str | None = Field(default=None, max_length=30)
    # Human-readable label of the scope used in the cascade ("מחוז צפון",
    # "תוכנית סל-טק", "כל הארגון"). Surfaced verbatim on the calendar so
    # 100 events sharing a series collapse into one chip with this label.
    scope_label: str | None = Field(default=None, max_length=120)
    recurrence: str | None = Field(
        default=None,
        description="Currently only 'weekly' is supported. Omit for a single occurrence.",
    )
    recurrence_until: date_type | None = Field(
        default=None,
        description="Inclusive end date for the recurrence (last week's session falls on this date or earlier).",
    )
    # Phase 15 — optional per-event extras. All go into attributes_json on
    # write and project back to top-level fields on read. Manager UI can
    # ignore them; coach UI (Phase 15.3) populates them.
    event_type_custom: str | None = Field(
        default=None,
        max_length=15,
        description="Free-text label when kind=='other'.",
    )
    opponent_home: str | None = Field(default=None, max_length=80)
    opponent_away: str | None = Field(default=None, max_length=80)


class PracticeUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    scheduled_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=0, le=600)
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    status: str | None = Field(default=None, max_length=30)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope_for_membership(
    membership: UserOrganization,
) -> dict[str, int | None]:
    """Map (role, scope columns) -> kwargs for the range/today queries."""
    role = membership.role
    org_id = membership.organization_id
    if role == "org_admin":
        return {"organization_id": org_id}
    if role == "program_manager":
        return {
            "organization_id": org_id,
            "program_id": getattr(membership, "program_id", None),
        }
    if role == "region_manager":
        return {
            "organization_id": org_id,
            "region_id": membership.region_id,
        }
    if role == "coach":
        return {
            "organization_id": org_id,
            "user_id": membership.user_id,
        }
    return {"organization_id": org_id}


def _serialize(row: dict) -> dict:
    """Shape the repo dict for JSON: datetimes -> ISO strings."""
    out = dict(row)
    scheduled = row.get("scheduled_at")
    if isinstance(scheduled, datetime):
        out["scheduled_at"] = scheduled.isoformat()
    return out


_WRITE_ROLES: frozenset[str] = frozenset({
    "org_admin", "program_manager", "region_manager", "branch_manager",
})


def _require_write(membership: UserOrganization) -> None:
    """Coach + viewer are read-only on the calendar. Cloak as 404 so a
    coach probing the write endpoints can't enumerate them."""
    if membership.role not in _WRITE_ROLES:
        raise NotFoundError("Practice session not found")


async def _resolve_team_scope(
    db: AsyncSession,
    team_id: int,
    membership: UserOrganization,
) -> tuple[TeamProfile, int | None, int | None]:
    """Return (team, effective_region_id, program_id).

    Verifies the team belongs to the org AND that the actor can write to
    it under their role. Raises 404 on any cross-scope hit (same cloak
    rule used everywhere else).
    """
    team = (await db.execute(
        select(TeamProfile).where(
            TeamProfile.id == team_id,
            TeamProfile.organization_id == membership.organization_id,
        )
    )).scalar_one_or_none()
    if team is None:
        raise NotFoundError("Team not found")

    # Effective region: direct FK first, else via branch.
    effective_region_id = team.region_id
    if effective_region_id is None and team.branch_id is not None:
        branch = await db.get(Branch, team.branch_id)
        effective_region_id = branch.region_id if branch else None

    # Phase 12 — program now reads directly off the team, not via the
    # region. Used to stamp the practice row's program_id so cross-program
    # filters (e.g. dashboard breakdown) stay consistent.
    program_id: int | None = team.program_id

    role = membership.role
    if role == "program_manager":
        pm_program = getattr(membership, "program_id", None)
        if pm_program is None or team.program_id != pm_program:
            raise NotFoundError("Team not found")
    elif role == "region_manager":
        if effective_region_id != membership.region_id:
            raise NotFoundError("Team not found")
        # Phase 12 — pinned-program RM further narrows by team.program_id.
        rm_program = getattr(membership, "program_id", None)
        if rm_program is not None and team.program_id != rm_program:
            raise NotFoundError("Team not found")
    elif role == "branch_manager":
        if team.branch_id != membership.branch_id:
            raise NotFoundError("Team not found")
    # org_admin: no further check.

    return team, effective_region_id, program_id


def _build_session(
    *,
    team: TeamProfile,
    region_id: int | None,
    program_id: int | None,
    body: PracticeCreate,
    scheduled_at: datetime,
    series_id: str | None,
    is_recurring: bool = False,
    scope_label: str | None = None,
    scope_count: int | None = None,
) -> PracticeSession:
    """Construct a PracticeSession row from a request body + a specific
    occurrence's `scheduled_at` (may differ from body's value for week 2+
    of a recurring series)."""
    attrs: dict | None = None
    kind = (body.kind or "practice").strip().lower()
    if kind not in _VALID_KINDS:
        kind = "other"
    # Phase 15 extras — only persist if non-empty so attributes_json stays
    # clean for the simple-practice path.
    has_phase15_extras = any([
        body.event_type_custom,
        body.opponent_home,
        body.opponent_away,
    ])
    if series_id is not None or kind != "practice" or scope_label or has_phase15_extras:
        attrs = {}
        if series_id is not None:
            attrs["series_id"] = series_id
        if is_recurring:
            attrs["recurrence"] = "weekly"
        attrs["kind"] = kind
        if scope_label:
            attrs["scope_label"] = scope_label
        if scope_count is not None and scope_count > 1:
            attrs["scope_count"] = scope_count
        if body.event_type_custom:
            attrs["event_type_custom"] = body.event_type_custom.strip()[:15]
        if body.opponent_home:
            attrs["opponent_home"] = body.opponent_home.strip()
        if body.opponent_away:
            attrs["opponent_away"] = body.opponent_away.strip()
    return PracticeSession(
        team_id=team.id,
        user_id=team.user_id,
        organization_id=team.organization_id,
        region_id=region_id,
        program_id=program_id,
        title=body.title,
        scheduled_at=scheduled_at,
        duration_minutes=body.duration_minutes,
        location=body.location,
        notes=body.notes,
        attributes_json=attrs,
        status="scheduled",
    )


# ---------------------------------------------------------------------------
# GET /org/api/practice/today  (Phase 3 — unchanged)
# ---------------------------------------------------------------------------


@router.get("/today")
async def list_today(
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
    day: str | None = Query(default=None),
) -> dict:
    target_day: date_type | None = None
    if day:
        try:
            target_day = date_type.fromisoformat(day)
        except ValueError:
            target_day = None

    scope = _scope_for_membership(membership)
    repo = PracticeSessionsRepository(db)
    rows = await repo.list_today_for_scope(day=target_day, **scope)

    return {
        "day": (target_day or date_type.today()).isoformat(),
        "scope": {
            "role": membership.role,
            "organization_id": membership.organization_id,
            "program_id": getattr(membership, "program_id", None),
            "region_id": membership.region_id,
        },
        "sessions": [_serialize(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# GET /org/api/practice/range
# ---------------------------------------------------------------------------


@router.get("/range")
async def list_range(
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
    start: str = Query(..., description="Inclusive start date (YYYY-MM-DD)."),
    end: str = Query(..., description="Exclusive end date (YYYY-MM-DD)."),
) -> dict:
    """Practices visible to the caller within `[start, end)`.

    The calendar grid passes `first-of-month - 6` and `last-of-month + 6`
    so the leading/trailing days from adjacent months render too.
    """
    try:
        start_d = date_type.fromisoformat(start)
        end_d = date_type.fromisoformat(end)
    except ValueError as e:
        raise ValidationError("start/end must be ISO dates", code="invalid_date") from e
    if end_d <= start_d:
        raise ValidationError("end must be after start", code="invalid_range")
    if (end_d - start_d).days > 120:
        # Hard cap so a misconfigured client can't pull years of data at once.
        raise ValidationError(
            "Range too large (max 120 days).", code="range_too_large",
        )

    scope = _scope_for_membership(membership)
    repo = PracticeSessionsRepository(db)
    rows = await repo.list_in_range_for_scope(start=start_d, end=end_d, **scope)
    return {
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "scope": {
            "role": membership.role,
            "organization_id": membership.organization_id,
            "program_id": getattr(membership, "program_id", None),
            "region_id": membership.region_id,
        },
        "can_write": membership.role in _WRITE_ROLES,
        "sessions": [_serialize(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# POST /org/api/practice — create (single + recurring weekly)
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_practice(
    body: PracticeCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    _require_write(membership)

    # Resolve which teams to schedule for. Either exactly one (team_id)
    # or many (team_ids). Each id is verified against the actor's scope —
    # passing a team outside scope yields 404 (cloak) even in bulk mode.
    if body.team_id is None and not body.team_ids:
        raise ValidationError(
            "Either team_id or team_ids is required.", code="team_required",
        )
    if body.team_id is not None and body.team_ids:
        raise ValidationError(
            "Pass team_id OR team_ids — not both.", code="ambiguous_team_target",
        )

    resolved_teams: list[tuple[TeamProfile, int | None, int | None]] = []
    if body.team_ids:
        # De-dup defensively (frontend should send unique ids).
        seen: set[int] = set()
        for tid in body.team_ids:
            if tid in seen:
                continue
            seen.add(tid)
            resolved_teams.append(await _resolve_team_scope(db, tid, membership))
        if len(resolved_teams) > 1000:
            raise ValidationError(
                "Too many teams in one bulk create (max 1000).",
                code="bulk_too_large",
            )
    else:
        resolved_teams.append(
            await _resolve_team_scope(db, body.team_id, membership)
        )

    occurrences: list[datetime] = [body.scheduled_at]
    series_id: str | None = None
    is_recurring = False

    if body.recurrence is not None:
        if body.recurrence != "weekly":
            raise ValidationError(
                "Unsupported recurrence (only 'weekly' is allowed in V1).",
                code="invalid_recurrence",
            )
        if body.recurrence_until is None:
            raise ValidationError(
                "recurrence_until is required for recurring sessions.",
                code="recurrence_until_required",
            )
        is_recurring = True
        occurrences = []
        cursor = body.scheduled_at
        end_of_day = datetime.combine(body.recurrence_until, datetime.max.time())
        # Cap to ~2 years (104 weeks) defensively.
        for _ in range(104):
            if cursor > end_of_day:
                break
            occurrences.append(cursor)
            cursor = cursor + timedelta(days=7)
        if not occurrences:
            raise ValidationError(
                "recurrence_until is before scheduled_at — nothing to create.",
                code="empty_recurrence",
            )

    # Series_id is assigned whenever the create fans out into more than one
    # row — either bulk teams, weekly recurrence, or both. The calendar
    # frontend groups by series_id so 100 rows on the same day collapse
    # into a single chip.
    bulk_teams = len(resolved_teams) > 1
    if bulk_teams or is_recurring:
        series_id = secrets.token_hex(6)

    # Hard cap on the cross-product to avoid runaway inserts.
    total_rows = len(resolved_teams) * len(occurrences)
    if total_rows > 1000:
        raise ValidationError(
            f"This would create {total_rows} rows — too many. "
            "Narrow the teams or shorten the recurrence.",
            code="cross_product_too_large",
        )

    scope_count = len(resolved_teams) if bulk_teams else None
    created: list[PracticeSession] = []
    for team, region_id, program_id in resolved_teams:
        for occ_at in occurrences:
            ps = _build_session(
                team=team,
                region_id=region_id,
                program_id=program_id,
                body=body,
                scheduled_at=occ_at,
                series_id=series_id,
                is_recurring=is_recurring,
                scope_label=body.scope_label,
                scope_count=scope_count,
            )
            db.add(ps)
            created.append(ps)
    await db.flush()
    for ps in created:
        await db.refresh(ps)

    sample_team = resolved_teams[0][0]
    await log_org_action(
        db,
        organization_id=sample_team.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="practice.create",
        target_type="practice_session",
        target_id=created[0].id,
        request=request,
        extra={
            "team_ids": [t.id for (t, _r, _p) in resolved_teams],
            "team_count": len(resolved_teams),
            "occurrence_count": len(occurrences),
            "count": len(created),
            "series_id": series_id,
        },
    )

    return {
        "ok": True,
        "series_id": series_id,
        "count": len(created),
        "team_count": len(resolved_teams),
        "occurrence_count": len(occurrences),
        "sessions": [
            {
                "id": ps.id,
                "team_id": ps.team_id,
                "title": ps.title,
                "scheduled_at": ps.scheduled_at.isoformat() if ps.scheduled_at else None,
                "duration_minutes": ps.duration_minutes,
                "location": ps.location,
                "status": ps.status,
                "series_id": series_id,
            }
            for ps in created
        ],
    }


# ---------------------------------------------------------------------------
# PATCH /org/api/practice/{id} — update one occurrence
# ---------------------------------------------------------------------------


async def _load_writable(
    db: AsyncSession, session_id: int, membership: UserOrganization,
) -> PracticeSession:
    """Fetch a session and verify the actor can write to it. 404 cloak."""
    _require_write(membership)
    ps = await db.get(PracticeSession, session_id)
    if ps is None or ps.organization_id != membership.organization_id:
        raise NotFoundError("Practice session not found")
    # Re-verify scope via the owning team — the team's region/program is the
    # source of truth even if the session row's denorm columns drift.
    await _resolve_team_scope(db, ps.team_id, membership)
    return ps


@router.patch("/{session_id}")
async def update_practice(
    session_id: int,
    body: PracticeUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    ps = await _load_writable(db, session_id, membership)

    changes: dict = {}

    def _set(attr: str, new):
        old = getattr(ps, attr)
        if new != old:
            changes[attr] = {"from": str(old) if old is not None else None,
                             "to": str(new) if new is not None else None}
            setattr(ps, attr, new)

    if body.title is not None:
        _set("title", body.title)
    if body.scheduled_at is not None:
        _set("scheduled_at", body.scheduled_at)
    if body.duration_minutes is not None:
        _set("duration_minutes", body.duration_minutes)
    if body.location is not None:
        _set("location", body.location)
    if body.notes is not None:
        _set("notes", body.notes)
    if body.status is not None:
        _set("status", body.status)

    if changes:
        await db.flush()
        await db.refresh(ps)
        await log_org_action(
            db,
            organization_id=ps.organization_id,
            actor_user_id=request.state.user.id,
            actor_email=request.state.user.email,
            action="practice.update",
            target_type="practice_session",
            target_id=ps.id,
            request=request,
            extra=changes,
        )

    return {
        "ok": True,
        "id": ps.id,
        "team_id": ps.team_id,
        "title": ps.title,
        "scheduled_at": ps.scheduled_at.isoformat() if ps.scheduled_at else None,
        "duration_minutes": ps.duration_minutes,
        "location": ps.location,
        "status": ps.status,
    }


# ---------------------------------------------------------------------------
# DELETE /org/api/practice/{id}
# ---------------------------------------------------------------------------


@router.delete("/{session_id}")
async def delete_practice(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
    series: bool = Query(
        default=False,
        description=(
            "When true, also delete every future occurrence (scheduled_at >= "
            "this session's scheduled_at) that shares the same series_id. "
            "Past occurrences are kept so historical records stay intact."
        ),
    ),
) -> dict:
    ps = await _load_writable(db, session_id, membership)

    deleted_count = 0
    series_id: str | None = None
    if series and isinstance(ps.attributes_json, dict):
        series_id = ps.attributes_json.get("series_id")

    if series_id is not None:
        # Delete every future occurrence (incl. this one) in the series.
        future = (await db.execute(
            select(PracticeSession).where(
                PracticeSession.organization_id == ps.organization_id,
                PracticeSession.scheduled_at >= ps.scheduled_at,
            )
        )).scalars().all()
        for row in future:
            attrs = row.attributes_json or {}
            if attrs.get("series_id") == series_id:
                await db.delete(row)
                deleted_count += 1
    else:
        await db.delete(ps)
        deleted_count = 1
    await db.flush()

    await log_org_action(
        db,
        organization_id=ps.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="practice.delete",
        target_type="practice_session",
        target_id=session_id,
        request=request,
        extra={"series_id": series_id, "count": deleted_count},
    )

    return {"ok": True, "deleted": deleted_count, "series_id": series_id}


__all__ = ["router"]
