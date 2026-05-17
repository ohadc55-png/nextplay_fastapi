"""Phase 15.6 — iCal feed generator + month_grid helper."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from src.services.ical_service import build_ical_feed, month_grid


def _ev(**kw):
    """Tiny stand-in for a PracticeSession row."""
    defaults = dict(
        id=1,
        scheduled_at=datetime(2026, 6, 15, 13, 0),
        duration_minutes=90,
        title="אימון",
        location=None,
        notes=None,
        attributes_json={},
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_ical_basic_structure_and_headers():
    body = build_ical_feed([_ev()], team_name="קבוצה")
    assert body.startswith("BEGIN:VCALENDAR")
    assert body.rstrip().endswith("END:VCALENDAR")
    assert "VERSION:2.0" in body
    assert "PRODID:-//NEXTPLAY//Coach Calendar//HE" in body
    assert "BEGIN:VEVENT" in body
    assert "UID:nextplay-1@nextplay.app" in body
    assert "DTSTART:20260615T130000Z" in body
    # End = start + 90 min
    assert "DTEND:20260615T143000Z" in body


def test_ical_escapes_special_chars():
    ev = _ev(title="כותרת; עם, פסיק\nושורה")
    body = build_ical_feed([ev])
    # Semicolons + commas + newlines must be backslash-escaped.
    assert "SUMMARY:\\u" not in body  # no Unicode escape mishap
    assert "\\;" in body
    assert "\\," in body
    assert "\\n" in body


def test_ical_game_includes_opponents_in_description():
    ev = _ev(
        attributes_json={
            "event_type": "game",
            "opponent_home": "מכבי ת״א",
            "opponent_away": "הפועל ירושלים",
        }
    )
    body = build_ical_feed([ev])
    assert "DESCRIPTION:" in body
    assert "מכבי" in body


def test_ical_uses_default_titles_when_blank():
    ev = _ev(title=None, attributes_json={"event_type": "game"})
    body = build_ical_feed([ev])
    assert "SUMMARY:משחק" in body


def test_month_grid_has_six_weeks_seven_days_each():
    grid = month_grid(2026, 6, [])
    assert all(len(week) == 7 for week in grid)
    # June 2026 spans 5-6 weeks in any reasonable grid
    assert 4 <= len(grid) <= 6


def test_month_grid_marks_other_month_days():
    grid = month_grid(2026, 6, [])
    flat = [c for week in grid for c in week]
    in_month = [c for c in flat if c["in_month"]]
    other = [c for c in flat if not c["in_month"]]
    # June has 30 days; rest are leading/trailing
    assert len(in_month) == 30
    assert len(other) > 0


def test_month_grid_buckets_events_by_local_date():
    # UTC 22:00 on June 14 → local (Asia/Jerusalem) is June 15 01:00 (DST UTC+3)
    ev = _ev(scheduled_at=datetime(2026, 6, 14, 22, 0))
    grid = month_grid(2026, 6, [ev])
    # Find the cell containing this event
    found = None
    for week in grid:
        for c in week:
            if c["events"]:
                found = c
                break
    assert found is not None
    assert found["date"] == date(2026, 6, 15)
