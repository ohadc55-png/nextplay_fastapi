"""Phase 15 — iCal feed + monthly share helpers.

Two public functions:

  * `build_ical_feed(events, *, team_name)` returns a `text/calendar`
    string conforming to RFC 5545. Times are UTC (NEXTPLAY stores UTC;
    iCal clients re-render in the subscriber's local TZ).
  * `month_grid(year, month, events)` returns a list of weeks for the
    public share page — each week is 7 cells, each cell is a dict with
    `date`, `in_month`, and `events`.

Both helpers are pure (no DB, no I/O). Callers fetch events via
`PracticeSessionsRepository.list_for_team()` or
`list_in_range_for_scope()` and pass them in.

iCal generator avoids any external library — vanilla string templating
keeps the dependency tree small and avoids `icalendar`'s ~1.5 MB pull.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

# Folding at 75 chars per RFC 5545 §3.1; clients tolerate longer lines
# but Apple Calendar is picky about HE Hebrew + non-ASCII.
_LINE_LIMIT = 73  # leave 2 chars margin for CRLF + leading space


def _escape(value: str) -> str:
    """Per RFC 5545 §3.3.11 — escape comma, semicolon, backslash, newline."""
    if value is None:
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


def _fold(line: str) -> str:
    """Long lines split with CRLF + space, per RFC 5545 §3.1."""
    if len(line) <= _LINE_LIMIT:
        return line
    parts = [line[:_LINE_LIMIT]]
    rest = line[_LINE_LIMIT:]
    while rest:
        parts.append(rest[:_LINE_LIMIT - 1])
        rest = rest[_LINE_LIMIT - 1:]
    return "\r\n ".join(parts)


def _fmt_dt(dt: datetime) -> str:
    """UTC datetime → YYYYMMDDTHHMMSSZ."""
    if dt.tzinfo is not None:
        # Treat as UTC; iCal "Z" means UTC.
        dt = dt.replace(tzinfo=None)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def build_ical_feed(events: Iterable[Any], *, team_name: str = "NEXTPLAY") -> str:
    """Build an RFC 5545 VCALENDAR string.

    `events` is an iterable of objects with:
      - id: int
      - scheduled_at: datetime (UTC)
      - duration_minutes: int | None
      - title: str | None
      - location: str | None
      - attributes_json: dict | None  (for event_type / opponents)
    """
    safe_name = _escape(team_name or "NEXTPLAY")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//NEXTPLAY//Coach Calendar//HE",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{safe_name}",
        "X-WR-TIMEZONE:Asia/Jerusalem",
    ]

    now_stamp = _fmt_dt(datetime.utcnow())
    for ev in events:
        attrs = getattr(ev, "attributes_json", None) or {}
        if not isinstance(attrs, dict):
            attrs = {}
        et = attrs.get("event_type") or attrs.get("kind") or "practice"
        title = ev.title or {"practice": "אימון", "game": "משחק", "event": "אירוע",
                             "other": attrs.get("event_type_custom") or "אירוע"}[
                                 et if et in ("practice", "game", "event", "other") else "practice"]
        duration = ev.duration_minutes or 90
        start = ev.scheduled_at
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace("Z", ""))
        end = start + timedelta(minutes=duration)

        ev_lines = [
            "BEGIN:VEVENT",
            f"UID:nextplay-{ev.id}@nextplay.app",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART:{_fmt_dt(start)}",
            f"DTEND:{_fmt_dt(end)}",
            _fold(f"SUMMARY:{_escape(title)}"),
        ]
        if ev.location:
            ev_lines.append(_fold(f"LOCATION:{_escape(ev.location)}"))
        # Description: include opponents for games + custom label for others
        description_bits = []
        if et == "game":
            home = attrs.get("opponent_home")
            away = attrs.get("opponent_away")
            if home and away:
                description_bits.append(f"{home} נגד {away}")
        if ev.notes:
            description_bits.append(ev.notes)
        if description_bits:
            ev_lines.append(_fold(f"DESCRIPTION:{_escape(' · '.join(description_bits))}"))
        ev_lines.append("END:VEVENT")
        lines.extend(ev_lines)

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def month_grid(
    year: int, month: int, events: Iterable[Any], *, week_start: int = 6,
) -> list[list[dict[str, Any]]]:
    """Return a 6×7 grid of cells for the share-page month view.

    Each cell: `{"date": date, "in_month": bool, "events": [event, …]}`.
    `week_start=6` = Sunday (Israel convention; Python's calendar uses
    Monday=0, Sunday=6).

    Events bucketized by their local date (Asia/Jerusalem). Events with
    naive UTC scheduled_at are converted by adding +2h (DST-aware-ish —
    if you need exact, use zoneinfo; for the share page rounding by
    ~1h is acceptable since we only display the date, not the time at
    a sub-day boundary).
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Jerusalem")

    cal = calendar.Calendar(firstweekday=week_start)
    weeks = list(cal.monthdatescalendar(year, month))
    # Bucket events by local date.
    buckets: dict[date, list[Any]] = {}
    for ev in events:
        start = ev.scheduled_at
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace("Z", ""))
        # Assume UTC; convert to Asia/Jerusalem date.
        local = start.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        buckets.setdefault(local.date(), []).append(ev)

    grid: list[list[dict[str, Any]]] = []
    for week in weeks:
        cells = []
        for d in week:
            cells.append({
                "date": d,
                "in_month": d.month == month,
                "events": sorted(
                    buckets.get(d, []),
                    key=lambda e: e.scheduled_at if not isinstance(e.scheduled_at, str)
                    else datetime.fromisoformat(e.scheduled_at.replace("Z", "")),
                ),
            })
        grid.append(cells)
    return grid


__all__ = ["build_ical_feed", "month_grid"]
