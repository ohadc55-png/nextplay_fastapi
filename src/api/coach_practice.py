"""Coach Calendar endpoints under /api/coach/practice/* (Phase 15).

JWT-auth (Coach App pattern, NOT org session). Scoped strictly to teams
where `team_profile.user_id == coach.id` — a coach can only see and write
events on their own teams. Cross-tenant access returns 404 (matches the
404-not-403 convention).

Endpoints:
  - GET    /range?start=&end=&team_id=        month view
  - POST   /                                   create one event or a series
  - PATCH  /{id}?scope=this|series             edit one or the series
  - DELETE /{id}?scope=this|series|day_of_week delete one / series / day
  - POST   /{id}/attendance                    mark attendance (idempotent)
  - POST   /{id}/note                          attach a free-document note
  - POST   /{id}/attach-plan                   link a practice_plan entry

Plus team-token endpoints used by 15.6 (iCal + share page):
  - POST   /teams/{team_id}/ical-token         mint/rotate ical_token
  - POST   /teams/{team_id}/share-token        mint/rotate share_token

The recurrence model: a "series" is N independent PracticeSession rows
sharing the same `attributes_json.series_id` (existing convention from
Phase 12). For "edit this one only" semantics, the new
`parent_event_id` column links a divergent row back to the anchor — same
series_id, but `parent_event_id` not NULL.

Every state-changing action that involves an org-bound team writes an
org_audit_log row so Phase 16's salary UI can sum attendance events
without scanning the practice_sessions table.
"""

from __future__ import annotations

import logging
import secrets
from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.database import get_db
from src.core.exceptions import NotFoundError, ValidationError
from src.models.notebook import NotebookAttendance, NotebookEntry, NotebookEntryPlayer
from src.models.players import Player
from src.models.practice_sessions import PracticeSession
from src.models.teams import TeamProfile
from src.models.users import User
from src.repositories.practice_sessions_repo import PracticeSessionsRepository
from src.services.org_audit_service import log_org_action
from src.services.team_colors import ensure_team_color

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/coach", tags=["coach-calendar"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

_VALID_EVENT_TYPES: frozenset[str] = frozenset({"practice", "game", "event", "other"})
_VALID_ATTENDANCE_STATUSES: frozenset[str] = frozenset({"present", "absent", "late"})
_MAX_OCCURRENCES = 200  # safety cap on recurrence expansion
_DAYS_OF_WEEK_RANGE = range(7)  # 0=Sunday … 6=Saturday (Hebrew week start)


class CoachRecurrence(BaseModel):
    """Recurrence sub-spec — multi-day-of-week within a date range.

    `days_of_week` is a list of integers 0-6 (0=Sunday, matching Israel's
    week-start). The series repeats weekly: for every week between the
    anchor date and `until_date` (inclusive), the server emits a row for
    each day-of-week in the list whose calendar date falls in range.
    """

    until_date: date_type
    days_of_week: list[int] = Field(min_length=1, max_length=7)


class CoachEventCreate(BaseModel):
    team_id: int
    event_type: str = Field(default="practice", max_length=30)
    title: str | None = Field(default=None, max_length=200)
    scheduled_at: datetime  # interpreted as UTC; client converts from user TZ
    duration_minutes: int | None = Field(default=None, ge=0, le=600)
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    event_type_custom: str | None = Field(default=None, max_length=15)
    opponent_home: str | None = Field(default=None, max_length=80)
    opponent_away: str | None = Field(default=None, max_length=80)
    practice_plan_entry_id: int | None = None
    recurrence: CoachRecurrence | None = None


class CoachEventUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    scheduled_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=0, le=600)
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    event_type: str | None = Field(default=None, max_length=30)
    event_type_custom: str | None = Field(default=None, max_length=15)
    opponent_home: str | None = Field(default=None, max_length=80)
    opponent_away: str | None = Field(default=None, max_length=80)
    status: str | None = Field(default=None, max_length=30)
    practice_plan_entry_id: int | None = None


class AttendanceRow(BaseModel):
    player_id: int
    status: str = Field(default="present", max_length=20)
    note: str | None = Field(default=None, max_length=500)


class AttendanceMark(BaseModel):
    players: list[AttendanceRow] = Field(default_factory=list)


class FreeNote(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    content: str = Field(min_length=1, max_length=10000)


class AttachPlan(BaseModel):
    entry_id: int | None = None  # None to detach


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_coach_team(
    db: AsyncSession, *, team_id: int, coach: User
) -> TeamProfile:
    """Verify the team belongs to this coach. 404 on any miss.

    The Coach Calendar is strictly coach-owned: even if the coach happens
    to be an org member with cross-team visibility on the org dashboard,
    THIS endpoint set only gives them what's nailed to them via
    `team.user_id`. RM-scheduled events on their team are still visible
    via `list_for_coach` (which joins on team.user_id), but writes are
    one-coach-one-team.
    """
    team = (await db.execute(
        select(TeamProfile).where(
            TeamProfile.id == team_id,
            TeamProfile.user_id == coach.id,
        )
    )).scalar_one_or_none()
    if team is None:
        raise NotFoundError("Team not found")
    # Phase 15 — lazy-assign a palette color the first time we touch a
    # team in calendar context. Idempotent.
    await ensure_team_color(db, team)
    return team


async def _resolve_coach_event(
    db: AsyncSession, *, event_id: int, coach: User
) -> tuple[PracticeSession, TeamProfile]:
    """Load an event scoped to coach-owned teams. 404 on any mismatch."""
    row = (await db.execute(
        select(PracticeSession, TeamProfile)
        .join(TeamProfile, TeamProfile.id == PracticeSession.team_id)
        .where(
            PracticeSession.id == event_id,
            TeamProfile.user_id == coach.id,
        )
    )).first()
    if row is None:
        raise NotFoundError("Event not found")
    event, team = row
    return event, team


def _build_attributes(
    *,
    event_type: str,
    event_type_custom: str | None = None,
    opponent_home: str | None = None,
    opponent_away: str | None = None,
    series_id: str | None = None,
    recurring: bool = False,
) -> dict[str, Any] | None:
    """Pack the per-event extras into attributes_json. Keep the dict
    minimal — only non-default fields go in so simple practice rows stay
    cheap."""
    et = (event_type or "practice").strip().lower()
    if et not in _VALID_EVENT_TYPES:
        et = "other"
    attrs: dict[str, Any] = {}
    if et != "practice":
        attrs["event_type"] = et
        attrs["kind"] = et  # back-compat mirror for manager UI readers
    if series_id is not None:
        attrs["series_id"] = series_id
    if recurring:
        attrs["recurrence"] = "weekly"
    if event_type_custom and et == "other":
        attrs["event_type_custom"] = event_type_custom.strip()[:15]
    if opponent_home and et == "game":
        attrs["opponent_home"] = opponent_home.strip()
    if opponent_away and et == "game":
        attrs["opponent_away"] = opponent_away.strip()
    return attrs or None


def _expand_recurrence(
    *, anchor: datetime, until: date_type, days_of_week: list[int]
) -> list[datetime]:
    """Return the concrete datetimes for a multi-day-of-week recurrence.

    Walks day-by-day from `anchor.date()` up to `until` (inclusive),
    emitting one datetime per match. The hour/minute on each match is
    copied from `anchor`. Hard cap at `_MAX_OCCURRENCES`; raises
    ValidationError if exceeded.

    `days_of_week` uses Israeli convention: 0=Sunday, 6=Saturday. We
    convert to Python's `weekday()` (0=Monday) on the fly.
    """
    if not days_of_week:
        return [anchor]
    # Convert Israeli weekday (0=Sun, 1=Mon, …, 6=Sat) to Python's
    # `weekday()` (0=Mon, 1=Tue, …, 6=Sun). Mapping: python = (israeli-1) % 7.
    #   user 0 (Sun) -> 6 (Sun)
    #   user 1 (Mon) -> 0 (Mon)
    #   user 6 (Sat) -> 5 (Sat)
    wanted = {(d - 1) % 7 for d in days_of_week}
    occurrences: list[datetime] = []
    cursor = anchor.date()
    end = until
    while cursor <= end:
        if cursor.weekday() in wanted:
            occurrences.append(
                datetime.combine(cursor, anchor.time())
            )
        if len(occurrences) > _MAX_OCCURRENCES:
            raise ValidationError(
                f"Recurrence would create more than {_MAX_OCCURRENCES} events.",
                code="recurrence_too_long",
            )
        cursor += timedelta(days=1)
    return occurrences or [anchor]


async def _audit_if_org(
    db: AsyncSession,
    *,
    coach: User,
    team: TeamProfile,
    action: str,
    target_id: int,
    request: Request,
    extra: dict[str, Any] | None = None,
) -> None:
    """Audit-log only when the team belongs to an org. Private-coach
    actions skip the org_audit_logs table (it'd FK-fail on org_id=None).
    """
    if team.organization_id is None:
        return
    await log_org_action(
        db,
        organization_id=team.organization_id,
        actor_user_id=coach.id,
        actor_email=coach.email,
        action=action,
        target_type="practice_session",
        target_id=target_id,
        request=request,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# GET /api/coach/practice/range — month view
# ---------------------------------------------------------------------------


@router.get("/practice/range", response_model=dict)
async def list_coach_events(
    start: date_type = Query(...),
    end: date_type = Query(...),
    team_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    coach: User = Depends(get_current_user),
) -> dict:
    """List the coach's events between `start` (inclusive) and `end`
    (exclusive). Optional `team_id` filters to one of their teams.

    Auto-assigns palette colors to every distinct team encountered in the
    range so the JS doesn't need a second roundtrip.
    """
    if (end - start).days > 366:
        raise ValidationError("Range too wide (max 366 days).", code="range_too_wide")
    repo = PracticeSessionsRepository(db)
    rows = await repo.list_for_coach(coach.id, start=start, end=end, team_id=team_id)
    return {"events": rows}


# ---------------------------------------------------------------------------
# POST /api/coach/practice — create event (single or recurring)
# ---------------------------------------------------------------------------


@router.post("/practice", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_coach_event(
    body: CoachEventCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    coach: User = Depends(get_current_user),
) -> dict:
    team = await _resolve_coach_team(db, team_id=body.team_id, coach=coach)

    # Normalize event_type — only "other" admits a custom label.
    event_type = (body.event_type or "practice").strip().lower()
    if event_type not in _VALID_EVENT_TYPES:
        event_type = "other"
    if event_type == "game":
        if not body.opponent_home or not body.opponent_away:
            raise ValidationError(
                "Game events require both opponent_home and opponent_away.",
                code="game_requires_opponents",
            )
    if event_type == "other" and not body.event_type_custom:
        raise ValidationError(
            "Custom event type requires event_type_custom text.",
            code="custom_event_label_required",
        )

    # Expand to occurrences. Single event = list with one datetime.
    if body.recurrence is not None:
        for d in body.recurrence.days_of_week:
            if d not in _DAYS_OF_WEEK_RANGE:
                raise ValidationError(
                    f"Invalid day_of_week {d} (use 0=Sunday … 6=Saturday).",
                    code="invalid_day_of_week",
                )
        if body.recurrence.until_date < body.scheduled_at.date():
            raise ValidationError(
                "Recurrence until_date must be after the first event.",
                code="invalid_recurrence_range",
            )
        occurrences = _expand_recurrence(
            anchor=body.scheduled_at,
            until=body.recurrence.until_date,
            days_of_week=body.recurrence.days_of_week,
        )
        series_id = secrets.token_hex(6)
    else:
        occurrences = [body.scheduled_at]
        series_id = None

    attrs = _build_attributes(
        event_type=event_type,
        event_type_custom=body.event_type_custom,
        opponent_home=body.opponent_home,
        opponent_away=body.opponent_away,
        series_id=series_id,
        recurring=series_id is not None,
    )

    created_ids: list[int] = []
    for at in occurrences:
        row = PracticeSession(
            team_id=team.id,
            user_id=coach.id,
            organization_id=team.organization_id,
            program_id=team.program_id,
            region_id=team.region_id,
            title=body.title,
            scheduled_at=at,
            duration_minutes=body.duration_minutes,
            location=body.location,
            notes=body.notes,
            attributes_json=attrs,
            practice_plan_entry_id=body.practice_plan_entry_id,
            status="scheduled",
        )
        db.add(row)
        await db.flush()
        created_ids.append(row.id)

    await _audit_if_org(
        db,
        coach=coach,
        team=team,
        action="practice.create",
        target_id=created_ids[0],
        request=request,
        extra={
            "event_type": event_type,
            "occurrences": len(created_ids),
            "series_id": series_id,
        },
    )

    return {
        "ok": True,
        "ids": created_ids,
        "series_id": series_id,
        "count": len(created_ids),
    }


# ---------------------------------------------------------------------------
# PATCH /api/coach/practice/{id} — edit (this only / series)
# ---------------------------------------------------------------------------


@router.patch("/practice/{event_id}", response_model=dict)
async def update_coach_event(
    event_id: int,
    body: CoachEventUpdate,
    request: Request,
    scope: Literal["this", "series"] = Query(default="this"),
    db: AsyncSession = Depends(get_db),
    coach: User = Depends(get_current_user),
) -> dict:
    event, team = await _resolve_coach_event(db, event_id=event_id, coach=coach)

    def apply(target: PracticeSession) -> None:
        if body.title is not None:
            target.title = body.title
        if body.scheduled_at is not None:
            target.scheduled_at = body.scheduled_at
        if body.duration_minutes is not None:
            target.duration_minutes = body.duration_minutes
        if body.location is not None:
            target.location = body.location
        if body.notes is not None:
            target.notes = body.notes
        if body.status is not None:
            target.status = body.status
        if body.practice_plan_entry_id is not None:
            target.practice_plan_entry_id = body.practice_plan_entry_id
        # If any attributes-json field changed, merge into existing dict.
        attrs = dict(target.attributes_json or {})
        if body.event_type is not None:
            et = body.event_type.strip().lower()
            if et not in _VALID_EVENT_TYPES:
                et = "other"
            attrs["event_type"] = et
            attrs["kind"] = et
        if body.event_type_custom is not None:
            attrs["event_type_custom"] = body.event_type_custom.strip()[:15]
        if body.opponent_home is not None:
            attrs["opponent_home"] = body.opponent_home.strip()
        if body.opponent_away is not None:
            attrs["opponent_away"] = body.opponent_away.strip()
        target.attributes_json = attrs or None

    if scope == "series":
        series_id = (event.attributes_json or {}).get("series_id")
        if not series_id:
            apply(event)
            await db.flush()
            updated_ids = [event.id]
        else:
            rows = list((await db.execute(
                select(PracticeSession)
                .join(TeamProfile, TeamProfile.id == PracticeSession.team_id)
                .where(
                    TeamProfile.user_id == coach.id,
                    PracticeSession.scheduled_at >= event.scheduled_at,
                )
            )).scalars().all())
            rows = [
                r for r in rows
                if (r.attributes_json or {}).get("series_id") == series_id
            ]
            for r in rows:
                apply(r)
            await db.flush()
            updated_ids = [r.id for r in rows]
    else:
        # scope == "this" — if event is part of a series, mark the row as
        # a divergent child by pointing parent_event_id at the original
        # series anchor (the earliest row sharing series_id). This keeps
        # the row in the series visually while preserving "edit this one
        # only" semantics for any subsequent series-wide edits.
        series_id = (event.attributes_json or {}).get("series_id")
        if series_id and event.parent_event_id is None:
            anchor = (await db.execute(
                select(PracticeSession)
                .join(TeamProfile, TeamProfile.id == PracticeSession.team_id)
                .where(
                    TeamProfile.user_id == coach.id,
                    PracticeSession.attributes_json.isnot(None),
                )
                .order_by(PracticeSession.scheduled_at.asc())
            )).scalars().all()
            anchor_row = next(
                (r for r in anchor
                 if (r.attributes_json or {}).get("series_id") == series_id
                 and r.id != event.id),
                None,
            )
            if anchor_row is not None:
                event.parent_event_id = anchor_row.id
        apply(event)
        await db.flush()
        updated_ids = [event.id]

    await _audit_if_org(
        db,
        coach=coach,
        team=team,
        action="practice.update",
        target_id=event.id,
        request=request,
        extra={"scope": scope, "count": len(updated_ids)},
    )
    return {"ok": True, "updated": updated_ids, "scope": scope}


# ---------------------------------------------------------------------------
# DELETE /api/coach/practice/{id} — single / series / day-of-week bulk
# ---------------------------------------------------------------------------


@router.delete("/practice/{event_id}", response_model=dict)
async def delete_coach_event(
    event_id: int,
    request: Request,
    scope: Literal["this", "series", "day_of_week"] = Query(default="this"),
    db: AsyncSession = Depends(get_db),
    coach: User = Depends(get_current_user),
) -> dict:
    event, team = await _resolve_coach_event(db, event_id=event_id, coach=coach)

    if scope == "this":
        await db.delete(event)
        await db.flush()
        deleted = 1
    elif scope == "series":
        series_id = (event.attributes_json or {}).get("series_id")
        if not series_id:
            await db.delete(event)
            await db.flush()
            deleted = 1
        else:
            rows = list((await db.execute(
                select(PracticeSession)
                .join(TeamProfile, TeamProfile.id == PracticeSession.team_id)
                .where(TeamProfile.user_id == coach.id)
            )).scalars().all())
            rows = [
                r for r in rows
                if (r.attributes_json or {}).get("series_id") == series_id
            ]
            for r in rows:
                await db.delete(r)
            await db.flush()
            deleted = len(rows)
    else:  # day_of_week — every future event for THIS team on the same weekday
        # Israel weekday: matching Python's weekday() of the anchor event.
        target_weekday = event.scheduled_at.weekday()
        rows = list((await db.execute(
            select(PracticeSession)
            .where(
                PracticeSession.team_id == team.id,
                PracticeSession.scheduled_at >= event.scheduled_at,
            )
        )).scalars().all())
        rows = [r for r in rows if r.scheduled_at.weekday() == target_weekday]
        for r in rows:
            await db.delete(r)
        await db.flush()
        deleted = len(rows)

    await _audit_if_org(
        db,
        coach=coach,
        team=team,
        action="practice.delete",
        target_id=event_id,
        request=request,
        extra={"scope": scope, "count": deleted},
    )
    return {"ok": True, "deleted": deleted, "scope": scope}


# ---------------------------------------------------------------------------
# POST /api/coach/practice/{id}/attendance — mark + audit
# ---------------------------------------------------------------------------


@router.post("/practice/{event_id}/attendance", response_model=dict)
async def mark_attendance(
    event_id: int,
    body: AttendanceMark,
    request: Request,
    db: AsyncSession = Depends(get_db),
    coach: User = Depends(get_current_user),
) -> dict:
    event, team = await _resolve_coach_event(db, event_id=event_id, coach=coach)

    # Validate player ids belong to this coach's team — defense in depth
    # (closure-based isolation but check before writing).
    submitted_ids = {row.player_id for row in body.players}
    if submitted_ids:
        valid_ids = set((await db.execute(
            select(Player.id).where(
                Player.id.in_(submitted_ids),
                Player.team_id == team.id,
                Player.user_id == coach.id,
            )
        )).scalars().all())
    else:
        valid_ids = set()
    rows = [r for r in body.players if r.player_id in valid_ids]

    # Find existing attendance entry for this session (idempotent replace).
    entry = (await db.execute(
        select(NotebookEntry).where(
            NotebookEntry.practice_session_id == event_id,
            NotebookEntry.entry_type == "attendance",
            NotebookEntry.user_id == coach.id,
        )
    )).scalar_one_or_none()

    if entry is None:
        entry = NotebookEntry(
            user_id=coach.id,
            team_id=team.id,
            entry_type="attendance",
            title=event.title or "נוכחות",
            entry_date=event.scheduled_at.date().isoformat(),
            content_json={"source": "calendar"},
            practice_session_id=event_id,
            source="calendar",
        )
        db.add(entry)
        await db.flush()
    else:
        # Replace existing attendance + player rows (idempotent).
        await db.execute(
            delete(NotebookAttendance).where(NotebookAttendance.entry_id == entry.id)
        )
        await db.execute(
            delete(NotebookEntryPlayer).where(NotebookEntryPlayer.entry_id == entry.id)
        )

    present_count = 0
    for row in rows:
        status_val = row.status.strip().lower()
        if status_val not in _VALID_ATTENDANCE_STATUSES:
            status_val = "present"
        if status_val == "present":
            present_count += 1
        db.add(NotebookAttendance(
            entry_id=entry.id,
            player_id=row.player_id,
            status=status_val,
            note=row.note or "",
        ))
        db.add(NotebookEntryPlayer(
            entry_id=entry.id,
            player_id=row.player_id,
        ))
    await db.flush()

    # Phase 16-ready audit row.
    await _audit_if_org(
        db,
        coach=coach,
        team=team,
        action="attendance.marked",
        target_id=event_id,
        request=request,
        extra={
            "entry_id": entry.id,
            "players_total": len(rows),
            "players_present": present_count,
            "event_type": (event.attributes_json or {}).get("event_type", "practice"),
        },
    )

    return {
        "ok": True,
        "entry_id": entry.id,
        "players_marked": len(rows),
        "players_present": present_count,
    }


# ---------------------------------------------------------------------------
# POST /api/coach/practice/{id}/note — free document saved to notebook
# ---------------------------------------------------------------------------


@router.post("/practice/{event_id}/note", response_model=dict)
async def attach_free_note(
    event_id: int,
    body: FreeNote,
    request: Request,
    db: AsyncSession = Depends(get_db),
    coach: User = Depends(get_current_user),
) -> dict:
    event, team = await _resolve_coach_event(db, event_id=event_id, coach=coach)

    entry = NotebookEntry(
        user_id=coach.id,
        team_id=team.id,
        entry_type="free_document",
        title=body.title or (event.title or "סיכום אימון"),
        entry_date=event.scheduled_at.date().isoformat(),
        content_json={"text": body.content, "source": "calendar"},
        practice_session_id=event_id,
        source="calendar",
    )
    db.add(entry)
    await db.flush()

    await _audit_if_org(
        db,
        coach=coach,
        team=team,
        action="practice.note_added",
        target_id=event_id,
        request=request,
        extra={"entry_id": entry.id, "length": len(body.content)},
    )

    return {"ok": True, "entry_id": entry.id}


# ---------------------------------------------------------------------------
# POST /api/coach/practice/{id}/attach-plan — link practice_plan entry
# ---------------------------------------------------------------------------


@router.post("/practice/{event_id}/attach-plan", response_model=dict)
async def attach_practice_plan(
    event_id: int,
    body: AttachPlan,
    request: Request,
    db: AsyncSession = Depends(get_db),
    coach: User = Depends(get_current_user),
) -> dict:
    event, team = await _resolve_coach_event(db, event_id=event_id, coach=coach)

    if body.entry_id is None:
        event.practice_plan_entry_id = None
        await db.flush()
        return {"ok": True, "attached": None}

    # Verify the entry belongs to this coach + is a practice_plan.
    plan = (await db.execute(
        select(NotebookEntry).where(
            NotebookEntry.id == body.entry_id,
            NotebookEntry.user_id == coach.id,
            NotebookEntry.entry_type == "practice_plan",
        )
    )).scalar_one_or_none()
    if plan is None:
        raise NotFoundError("Practice plan not found")

    event.practice_plan_entry_id = plan.id
    await db.flush()

    await _audit_if_org(
        db,
        coach=coach,
        team=team,
        action="practice.plan_attached",
        target_id=event_id,
        request=request,
        extra={"plan_entry_id": plan.id},
    )

    return {"ok": True, "attached": plan.id}


# ---------------------------------------------------------------------------
# Sharing tokens — iCal feed + public share page (Phase 15.6 uses these)
# ---------------------------------------------------------------------------


@router.post("/teams/{team_id}/ical-token", response_model=dict)
async def mint_ical_token(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    coach: User = Depends(get_current_user),
) -> dict:
    team = await _resolve_coach_team(db, team_id=team_id, coach=coach)
    team.ical_token = secrets.token_urlsafe(32)
    await db.flush()
    return {
        "ok": True,
        "url": f"/calendar/feed/{team.ical_token}.ics",
        "token": team.ical_token,
    }


@router.post("/teams/{team_id}/share-token", response_model=dict)
async def mint_share_token(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    coach: User = Depends(get_current_user),
) -> dict:
    team = await _resolve_coach_team(db, team_id=team_id, coach=coach)
    team.share_token = secrets.token_urlsafe(32)
    await db.flush()
    return {
        "ok": True,
        "url": f"/calendar/share/{team.share_token}",
        "token": team.share_token,
    }


__all__ = ["router"]
