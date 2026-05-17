"""Practice sessions repository — backs the "today's training" sidebar.

The org dashboards (org_admin / program_manager / region_manager) each
need the same query — practices scheduled for today, scoped to whatever
slice of the org the viewer can see — so the repo exposes one
`list_today_for_scope` that takes the scope axes as kwargs and applies
them as AND filters. Missing scope axes (None) skip the filter; passing
*nothing* returns [] as a defensive guard against unscoped reads.

The query joins TeamProfile + User so callers don't need a second
roundtrip to render the row label ("Coach X — Team Y"). This pattern
mirrors `UserOrganizationsRepository.list_with_user_data`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.models.practice_sessions import PracticeSession
from src.models.teams import TeamProfile
from src.models.users import User
from src.repositories.base_repository import BaseRepository


class PracticeSessionsRepository(BaseRepository[PracticeSession]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, PracticeSession)

    async def list_today_for_scope(
        self,
        *,
        organization_id: int | None = None,
        program_id: int | None = None,
        region_id: int | None = None,
        team_id: int | None = None,
        user_id: int | None = None,
        day: date | None = None,
    ) -> list[dict[str, Any]]:
        """Return today's practices for a (possibly multi-axis) scope.

        At least one of the scope kwargs must be non-None — without any
        scope this returns [] rather than scanning the whole table.

        Each row is a flat dict so the route can serialize without
        touching the ORM relationships (which would lazy-load on a
        closed session)."""
        if all(
            v is None
            for v in (organization_id, program_id, region_id, team_id, user_id)
        ):
            return []

        target = day or date.today()
        start = datetime.combine(target, datetime.min.time())
        end = start + timedelta(days=1)

        Coach = aliased(User)
        stmt = (
            select(PracticeSession, TeamProfile, Coach)
            .join(TeamProfile, TeamProfile.id == PracticeSession.team_id)
            .outerjoin(Coach, Coach.id == PracticeSession.user_id)
            .where(
                PracticeSession.scheduled_at >= start,
                PracticeSession.scheduled_at < end,
            )
            .order_by(PracticeSession.scheduled_at.asc())
        )
        if organization_id is not None:
            stmt = stmt.where(PracticeSession.organization_id == organization_id)
        if program_id is not None:
            stmt = stmt.where(PracticeSession.program_id == program_id)
        if region_id is not None:
            stmt = stmt.where(PracticeSession.region_id == region_id)
        if team_id is not None:
            stmt = stmt.where(PracticeSession.team_id == team_id)
        if user_id is not None:
            stmt = stmt.where(PracticeSession.user_id == user_id)

        rows = (await self.session.execute(stmt)).all()
        return [_serialize_row(p, t, c) for (p, t, c) in rows]

    async def list_in_range_for_scope(
        self,
        *,
        start: date,
        end: date,
        organization_id: int | None = None,
        program_id: int | None = None,
        region_id: int | None = None,
        team_id: int | None = None,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Same shape as `list_today_for_scope` but covers an arbitrary
        date range (inclusive `start`, exclusive `end`). Used by the
        calendar's month view fetch — caller passes the first and last
        day shown on the grid (including the leading/trailing days from
        adjacent months)."""
        if all(
            v is None
            for v in (organization_id, program_id, region_id, team_id, user_id)
        ):
            return []
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.min.time())

        Coach = aliased(User)
        stmt = (
            select(PracticeSession, TeamProfile, Coach)
            .join(TeamProfile, TeamProfile.id == PracticeSession.team_id)
            .outerjoin(Coach, Coach.id == PracticeSession.user_id)
            .where(
                PracticeSession.scheduled_at >= start_dt,
                PracticeSession.scheduled_at < end_dt,
            )
            .order_by(PracticeSession.scheduled_at.asc())
        )
        if organization_id is not None:
            stmt = stmt.where(PracticeSession.organization_id == organization_id)
        if program_id is not None:
            stmt = stmt.where(PracticeSession.program_id == program_id)
        if region_id is not None:
            stmt = stmt.where(PracticeSession.region_id == region_id)
        if team_id is not None:
            stmt = stmt.where(PracticeSession.team_id == team_id)
        if user_id is not None:
            stmt = stmt.where(PracticeSession.user_id == user_id)

        rows = (await self.session.execute(stmt)).all()
        return [_serialize_row(p, t, c) for (p, t, c) in rows]

    async def list_for_team(
        self,
        team_id: int | None,
        *,
        upcoming_only: bool = False,
        limit: int | None = 50,
    ) -> list[PracticeSession]:
        """All practices for one team. `upcoming_only=True` filters to
        sessions whose scheduled_at >= now. Defensive: team_id=None → []."""
        if team_id is None:
            return []
        stmt = select(PracticeSession).where(PracticeSession.team_id == team_id)
        if upcoming_only:
            stmt = stmt.where(PracticeSession.scheduled_at >= datetime.utcnow())
        stmt = stmt.order_by(PracticeSession.scheduled_at.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_coach(
        self,
        coach_user_id: int,
        *,
        start: date,
        end: date,
        team_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Phase 15 — Coach Calendar feed.

        Returns serialized events for any team the coach owns
        (`team.user_id == coach_user_id`). Optional `team_id` narrows to
        one of their teams (e.g. when the coach has 2 teams and uses the
        filter dropdown). Range is [start, end) on the date axis.

        Used by `GET /api/coach/practice/range`. Mirrors
        `list_in_range_for_scope` but joins through TeamProfile.user_id
        rather than PracticeSession.user_id — that way RM-created events
        (where `PracticeSession.user_id` may be NULL or someone else's)
        still show up if the team belongs to this coach.

        Phase 15.9 — also enriches each row with three has_* flags so the
        UI can render status badges on the card (does this event have
        attendance / a free note / a linked practice plan?). One bulk
        sub-query per flag — cheap because we're already in O(N) range.
        """
        from src.models.notebook import NotebookEntry

        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.min.time())

        Coach = aliased(User)
        stmt = (
            select(PracticeSession, TeamProfile, Coach)
            .join(TeamProfile, TeamProfile.id == PracticeSession.team_id)
            .outerjoin(Coach, Coach.id == PracticeSession.user_id)
            .where(
                TeamProfile.user_id == coach_user_id,
                PracticeSession.scheduled_at >= start_dt,
                PracticeSession.scheduled_at < end_dt,
            )
            .order_by(PracticeSession.scheduled_at.asc())
        )
        if team_id is not None:
            stmt = stmt.where(PracticeSession.team_id == team_id)
        rows = (await self.session.execute(stmt)).all()
        serialized = [_serialize_row(p, t, c) for (p, t, c) in rows]
        if not serialized:
            return serialized

        ids = [r["id"] for r in serialized]
        # Bulk lookup of notebook entries that describe these events.
        nb_rows = list((await self.session.execute(
            select(NotebookEntry)
            .where(
                NotebookEntry.practice_session_id.in_(ids),
                NotebookEntry.entry_type.in_(("attendance", "free_document")),
            )
        )).scalars().all())
        att_by_sess: dict[int, NotebookEntry] = {}
        note_by_sess: dict[int, NotebookEntry] = {}
        for nb in nb_rows:
            if nb.entry_type == "attendance":
                att_by_sess[nb.practice_session_id] = nb
            elif nb.entry_type == "free_document":
                note_by_sess[nb.practice_session_id] = nb

        # Pull attendance rows for the matched entries in one go.
        from src.models.notebook import NotebookAttendance
        from src.models.players import Player

        att_entry_ids = [e.id for e in att_by_sess.values()]
        att_rows_by_entry: dict[int, list[Any]] = {}
        player_names: dict[int, str] = {}
        if att_entry_ids:
            att_rows = list((await self.session.execute(
                select(NotebookAttendance).where(
                    NotebookAttendance.entry_id.in_(att_entry_ids)
                )
            )).scalars().all())
            for row in att_rows:
                att_rows_by_entry.setdefault(row.entry_id, []).append(row)
            player_ids = {row.player_id for row in att_rows}
            if player_ids:
                players = list((await self.session.execute(
                    select(Player.id, Player.name, Player.number)
                    .where(Player.id.in_(player_ids))
                )).all())
                for pid, name, _num in players:
                    player_names[pid] = name or "—"

        # Pull plan titles for events with a linked practice_plan entry.
        plan_ids = [r["practice_plan_entry_id"] for r in serialized
                    if r.get("practice_plan_entry_id")]
        plan_info: dict[int, dict[str, Any]] = {}
        if plan_ids:
            plan_rows = list((await self.session.execute(
                select(NotebookEntry.id, NotebookEntry.title, NotebookEntry.content_json)
                .where(NotebookEntry.id.in_(plan_ids))
            )).all())
            for pid, ptitle, pcontent in plan_rows:
                # Build a short text preview from content_json. Shape varies,
                # but most entries store a `text` string or a list of blocks.
                preview = ""
                if isinstance(pcontent, dict):
                    if isinstance(pcontent.get("text"), str):
                        preview = pcontent["text"]
                    elif isinstance(pcontent.get("summary"), str):
                        preview = pcontent["summary"]
                plan_info[pid] = {
                    "title": ptitle or "Practice plan",
                    "preview": preview[:280] if preview else "",
                }

        for r in serialized:
            sid = r["id"]
            r["has_attendance"] = sid in att_by_sess
            r["has_note"] = sid in note_by_sess
            r["has_plan"] = r.get("practice_plan_entry_id") is not None

            # Inline attendance — list of {player_id, name, status}
            if sid in att_by_sess:
                entry = att_by_sess[sid]
                rows = att_rows_by_entry.get(entry.id, [])
                r["attendance_entry_id"] = entry.id
                r["attendance"] = [
                    {
                        "player_id": a.player_id,
                        "name": player_names.get(a.player_id, "—"),
                        "status": a.status or "present",
                    }
                    for a in rows
                ]
                r["attendance_summary"] = {
                    "present": sum(1 for a in rows if a.status == "present"),
                    "absent":  sum(1 for a in rows if a.status == "absent"),
                    "late":    sum(1 for a in rows if a.status == "late"),
                    "total":   len(rows),
                }
            else:
                r["attendance"] = []
                r["attendance_summary"] = None
                r["attendance_entry_id"] = None

            # Inline free-document note — title + body text
            if sid in note_by_sess:
                note = note_by_sess[sid]
                body = ""
                if isinstance(note.content_json, dict):
                    body = note.content_json.get("text") or ""
                r["note"] = {
                    "entry_id": note.id,
                    "title": note.title,
                    "text": body,
                }
            else:
                r["note"] = None

            # Inline plan — title + short preview + entry_id
            if r.get("practice_plan_entry_id") and r["practice_plan_entry_id"] in plan_info:
                r["plan"] = {
                    "entry_id": r["practice_plan_entry_id"],
                    **plan_info[r["practice_plan_entry_id"]],
                }
            else:
                r["plan"] = None

        return serialized


def _serialize_row(
    p: PracticeSession, t: TeamProfile, coach: User | None
) -> dict[str, Any]:
    attrs = p.attributes_json or {}
    if not isinstance(attrs, dict):
        attrs = {}
    kind = attrs.get("kind") or attrs.get("event_type") or "practice"
    return {
        "id": p.id,
        "team_id": p.team_id,
        "team_name": t.team_name,
        "team_color_hex": t.color_hex,  # Phase 15 — palette chip color
        "organization_id": p.organization_id,
        "program_id": p.program_id,
        "region_id": p.region_id,
        "title": p.title,
        "scheduled_at": p.scheduled_at,
        "duration_minutes": p.duration_minutes,
        "location": p.location,
        "status": p.status,
        "notes": p.notes,
        "user_id": p.user_id,
        "coach_display_name": (coach.display_name if coach else None),
        "coach_email": (coach.email if coach else None),
        # Phase 12 — kind ("practice"/"game"/"meeting"/"other") + series_id
        # come out of attributes_json (no dedicated columns yet). scope_label
        # + scope_count let the calendar collapse bulk events into one chip.
        # Phase 15 — `event_type` is the canonical name going forward; we
        # mirror it both ways so old and new clients see what they expect.
        "kind": kind,
        "event_type": kind,
        "event_type_custom": attrs.get("event_type_custom"),
        "opponent_home": attrs.get("opponent_home"),
        "opponent_away": attrs.get("opponent_away"),
        "series_id": attrs.get("series_id"),
        "scope_label": attrs.get("scope_label"),
        "scope_count": attrs.get("scope_count"),
        # Phase 15 — practice plan link + edit-single-in-series parent.
        "practice_plan_entry_id": p.practice_plan_entry_id,
        "parent_event_id": p.parent_event_id,
    }


__all__ = ["PracticeSessionsRepository"]
