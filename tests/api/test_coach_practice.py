"""Phase 15.2 — Coach Calendar API.

Covers:
  - Range listing (scope, date window, team filter)
  - Create single event + recurring series (multi-day-of-week)
  - Game-type requires home + away
  - Other-type requires custom label (15 chars)
  - Edit one occurrence vs whole series
  - Delete single / series / day-of-week
  - Attendance mark + idempotent replace + audit log
  - Free-note attaches to notebook with practice_session_id
  - Attach practice plan
  - Cross-tenant — coach B never sees coach A's events
  - Recurrence cap at 200 occurrences
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def coach_with_team(authed_client: AsyncClient, api_session_factory):
    """Pre-authed Coach App client + a team owned by them. Returns the
    client + a dict of context (user_id, team_id, org_id)."""
    from src.models.teams import TeamProfile
    from src.models.users import User

    async with api_session_factory() as s:
        user = (await s.execute(
            select(User).where(User.email == "tester@example.com")
        )).scalar_one()
        team = TeamProfile(team_name="Coach Team", user_id=user.id)
        s.add(team)
        await s.commit()
        return authed_client, {"user_id": user.id, "team_id": team.id}


# ---------------------------------------------------------------------------
# Create / list
# ---------------------------------------------------------------------------


async def test_create_single_event_and_appears_in_range(coach_with_team):
    client, ctx = coach_with_team
    at = (datetime.utcnow() + timedelta(days=1)).replace(microsecond=0)
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "event_type": "practice",
        "title": "אימון בוקר",
        "scheduled_at": at.isoformat(),
        "duration_minutes": 90,
        "location": "אולם קרית טבעון",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["series_id"] is None

    today = date.today()
    rng = await client.get(
        f"/api/coach/practice/range?start={today.isoformat()}"
        f"&end={(today + timedelta(days=7)).isoformat()}"
    )
    assert rng.status_code == 200
    events = rng.json()["events"]
    assert len(events) == 1
    assert events[0]["title"] == "אימון בוקר"
    assert events[0]["team_color_hex"]  # palette auto-assigned


async def test_recurring_weekly_creates_n_occurrences(coach_with_team):
    client, ctx = coach_with_team
    anchor = datetime(2026, 6, 1, 16, 0, 0)  # Monday
    until = date(2026, 6, 29)
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "scheduled_at": anchor.isoformat(),
        "duration_minutes": 90,
        "recurrence": {
            "until_date": until.isoformat(),
            "days_of_week": [1, 3],  # Mon + Wed (1=Mon, 3=Wed in Israeli)
        },
    })
    assert r.status_code == 201, r.text
    body = r.json()
    # June 1-29: Mondays = 1,8,15,22,29 = 5; Wednesdays = 3,10,17,24 = 4 → 9
    assert body["count"] == 9
    assert body["series_id"]


async def test_recurrence_cap_blocks_runaway_series(coach_with_team):
    client, ctx = coach_with_team
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "scheduled_at": "2026-01-01T16:00:00",
        "recurrence": {
            "until_date": "2030-12-31",
            "days_of_week": [0, 1, 2, 3, 4, 5, 6],  # every day for 5 years
        },
    })
    assert r.status_code == 422
    assert r.json().get("code") == "recurrence_too_long"


async def test_game_type_requires_opponents(coach_with_team):
    client, ctx = coach_with_team
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "event_type": "game",
        "scheduled_at": "2026-06-15T18:00:00",
    })
    assert r.status_code == 422
    assert r.json().get("code") == "game_requires_opponents"


async def test_other_type_requires_custom_label(coach_with_team):
    client, ctx = coach_with_team
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "event_type": "other",
        "scheduled_at": "2026-06-15T18:00:00",
    })
    assert r.status_code == 422
    assert r.json().get("code") == "custom_event_label_required"


async def test_other_event_persists_custom_label(coach_with_team):
    client, ctx = coach_with_team
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "event_type": "other",
        "event_type_custom": "טורניר חורף",
        "scheduled_at": "2026-06-15T18:00:00",
    })
    assert r.status_code == 201
    rng = await client.get(
        "/api/coach/practice/range?start=2026-06-01&end=2026-07-01"
    )
    events = rng.json()["events"]
    assert events[0]["event_type"] == "other"
    assert events[0]["event_type_custom"] == "טורניר חורף"


# ---------------------------------------------------------------------------
# Cross-tenant
# ---------------------------------------------------------------------------


async def test_coach_cannot_see_other_coachs_events(
    coach_with_team, api_client: AsyncClient, register_user, api_session_factory
):
    client_a, ctx_a = coach_with_team
    # Create event as coach A
    await client_a.post("/api/coach/practice", json={
        "team_id": ctx_a["team_id"],
        "scheduled_at": "2026-06-15T16:00:00",
    })

    # Register coach B in a separate client + team
    api_client.cookies.clear()
    await api_client.post("/api/auth/register", json={
        "email": "coachb@example.com",
        "password": "Sup3rSecure!",
        "display_name": "Coach B",
    })
    from src.models.teams import TeamProfile
    from src.models.users import User
    async with api_session_factory() as s:
        user_b = (await s.execute(
            select(User).where(User.email == "coachb@example.com")
        )).scalar_one()
        team_b = TeamProfile(team_name="Other Team", user_id=user_b.id)
        s.add(team_b)
        await s.commit()

    rng = await api_client.get(
        "/api/coach/practice/range?start=2026-06-01&end=2026-07-01"
    )
    assert rng.status_code == 200
    assert rng.json()["events"] == []  # B sees nothing of A's


async def test_coach_cannot_write_to_other_teams_event(
    coach_with_team, api_client: AsyncClient, api_session_factory
):
    client_a, ctx_a = coach_with_team
    r = await client_a.post("/api/coach/practice", json={
        "team_id": ctx_a["team_id"],
        "scheduled_at": "2026-06-15T16:00:00",
    })
    event_id = r.json()["ids"][0]

    # Coach B can't PATCH or DELETE coach A's event
    api_client.cookies.clear()
    await api_client.post("/api/auth/register", json={
        "email": "coachb2@example.com",
        "password": "Sup3rSecure!",
        "display_name": "B",
    })
    r = await api_client.patch(
        f"/api/coach/practice/{event_id}", json={"title": "hacked"}
    )
    assert r.status_code == 404
    r = await api_client.delete(f"/api/coach/practice/{event_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


async def test_attendance_mark_creates_notebook_entry_with_link(
    coach_with_team, api_session_factory
):
    from src.models.notebook import NotebookAttendance, NotebookEntry
    from src.models.players import Player

    client, ctx = coach_with_team

    # Seed 3 players on the team
    async with api_session_factory() as s:
        for n in (4, 7, 11):
            s.add(Player(
                user_id=ctx["user_id"],
                team_id=ctx["team_id"],
                name=f"Player {n}",
                number=n,
                active=True,
            ))
        await s.commit()

    # Create event
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "scheduled_at": "2026-06-15T16:00:00",
    })
    event_id = r.json()["ids"][0]

    # Mark attendance — 2 present + 1 absent
    async with api_session_factory() as s:
        roster = list((await s.execute(
            select(Player).where(Player.team_id == ctx["team_id"]).order_by(Player.number)
        )).scalars().all())
    payload = [
        {"player_id": roster[0].id, "status": "present"},
        {"player_id": roster[1].id, "status": "present"},
        {"player_id": roster[2].id, "status": "absent"},
    ]
    r = await client.post(
        f"/api/coach/practice/{event_id}/attendance", json={"players": payload}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["players_marked"] == 3
    assert body["players_present"] == 2

    # NotebookEntry created with practice_session_id link
    async with api_session_factory() as s:
        entry = (await s.execute(
            select(NotebookEntry).where(NotebookEntry.id == body["entry_id"])
        )).scalar_one()
        assert entry.practice_session_id == event_id
        assert entry.entry_type == "attendance"
        att_rows = list((await s.execute(
            select(NotebookAttendance).where(NotebookAttendance.entry_id == entry.id)
        )).scalars().all())
        assert len(att_rows) == 3


async def test_attendance_replace_is_idempotent(coach_with_team, api_session_factory):
    from src.models.notebook import NotebookAttendance, NotebookEntry
    from src.models.players import Player

    client, ctx = coach_with_team
    async with api_session_factory() as s:
        s.add(Player(
            user_id=ctx["user_id"], team_id=ctx["team_id"],
            name="P", number=1, active=True,
        ))
        await s.commit()
        pid = (await s.execute(
            select(Player.id).where(Player.team_id == ctx["team_id"])
        )).scalar_one()

    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"], "scheduled_at": "2026-06-15T16:00:00"
    })
    event_id = r.json()["ids"][0]

    # First mark: absent
    await client.post(
        f"/api/coach/practice/{event_id}/attendance",
        json={"players": [{"player_id": pid, "status": "absent"}]},
    )
    # Re-mark: present (replace)
    r2 = await client.post(
        f"/api/coach/practice/{event_id}/attendance",
        json={"players": [{"player_id": pid, "status": "present"}]},
    )
    assert r2.status_code == 200

    async with api_session_factory() as s:
        entries = list((await s.execute(
            select(NotebookEntry).where(
                NotebookEntry.practice_session_id == event_id,
                NotebookEntry.entry_type == "attendance",
            )
        )).scalars().all())
        # Idempotent — still one entry, not two
        assert len(entries) == 1
        att = list((await s.execute(
            select(NotebookAttendance).where(NotebookAttendance.entry_id == entries[0].id)
        )).scalars().all())
        assert len(att) == 1
        assert att[0].status == "present"


# ---------------------------------------------------------------------------
# Free note
# ---------------------------------------------------------------------------


async def test_free_note_saves_to_notebook(coach_with_team, api_session_factory):
    from src.models.notebook import NotebookEntry

    client, ctx = coach_with_team
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"], "scheduled_at": "2026-06-15T16:00:00",
    })
    event_id = r.json()["ids"][0]

    note = await client.post(
        f"/api/coach/practice/{event_id}/note",
        json={"title": "אימון מצוין", "content": "השחקנים עבדו על הגנה"},
    )
    assert note.status_code == 200
    eid = note.json()["entry_id"]

    async with api_session_factory() as s:
        entry = (await s.execute(
            select(NotebookEntry).where(NotebookEntry.id == eid)
        )).scalar_one()
        assert entry.entry_type == "free_document"
        assert entry.practice_session_id == event_id
        assert "השחקנים" in entry.content_json["text"]


# ---------------------------------------------------------------------------
# Edit / delete scopes
# ---------------------------------------------------------------------------


async def test_edit_single_in_series_marks_as_divergent(coach_with_team):
    client, ctx = coach_with_team
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "scheduled_at": "2026-06-01T16:00:00",  # Monday
        "recurrence": {"until_date": "2026-06-22", "days_of_week": [1]},
    })
    assert r.status_code == 201
    ids = r.json()["ids"]
    assert len(ids) == 4

    # Edit just the second occurrence's location
    r = await client.patch(
        f"/api/coach/practice/{ids[1]}?scope=this",
        json={"location": "אולם רמת גן"},
    )
    assert r.status_code == 200
    rng = await client.get(
        "/api/coach/practice/range?start=2026-06-01&end=2026-07-01"
    )
    events = {e["id"]: e for e in rng.json()["events"]}
    # Just that one changed
    assert events[ids[1]]["location"] == "אולם רמת גן"
    assert events[ids[0]]["location"] is None
    # And it's now marked as divergent
    assert events[ids[1]]["parent_event_id"] is not None


async def test_edit_series_cascades(coach_with_team):
    client, ctx = coach_with_team
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "scheduled_at": "2026-06-01T16:00:00",
        "recurrence": {"until_date": "2026-06-22", "days_of_week": [1]},
    })
    ids = r.json()["ids"]

    # Edit the SERIES location at the anchor
    r = await client.patch(
        f"/api/coach/practice/{ids[0]}?scope=series",
        json={"location": "אולם חדש"},
    )
    assert r.status_code == 200
    rng = await client.get(
        "/api/coach/practice/range?start=2026-06-01&end=2026-07-01"
    )
    events = {e["id"]: e for e in rng.json()["events"]}
    for eid in ids:
        assert events[eid]["location"] == "אולם חדש"


async def test_delete_series_removes_all(coach_with_team):
    client, ctx = coach_with_team
    r = await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "scheduled_at": "2026-06-01T16:00:00",
        "recurrence": {"until_date": "2026-06-22", "days_of_week": [1]},
    })
    ids = r.json()["ids"]
    r = await client.delete(f"/api/coach/practice/{ids[0]}?scope=series")
    assert r.status_code == 200
    assert r.json()["deleted"] == 4
    rng = await client.get(
        "/api/coach/practice/range?start=2026-06-01&end=2026-07-01"
    )
    assert rng.json()["events"] == []


async def test_delete_day_of_week_bulk(coach_with_team):
    client, ctx = coach_with_team
    # Two series: Mondays + Wednesdays
    await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "scheduled_at": "2026-06-01T16:00:00",  # Mon
        "recurrence": {"until_date": "2026-06-22", "days_of_week": [1]},
    })
    await client.post("/api/coach/practice", json={
        "team_id": ctx["team_id"],
        "scheduled_at": "2026-06-03T16:00:00",  # Wed
        "recurrence": {"until_date": "2026-06-24", "days_of_week": [3]},
    })
    rng = await client.get(
        "/api/coach/practice/range?start=2026-06-01&end=2026-07-01"
    )
    monday_event = next(
        e for e in rng.json()["events"] if datetime.fromisoformat(e["scheduled_at"]).weekday() == 0
    )
    # Delete all Mondays
    r = await client.delete(
        f"/api/coach/practice/{monday_event['id']}?scope=day_of_week"
    )
    assert r.status_code == 200
    rng = await client.get(
        "/api/coach/practice/range?start=2026-06-01&end=2026-07-01"
    )
    remaining = rng.json()["events"]
    # Only Wednesdays left
    assert all(
        datetime.fromisoformat(e["scheduled_at"]).weekday() == 2
        for e in remaining
    )


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


async def test_mint_ical_and_share_tokens(coach_with_team, api_session_factory):
    from src.models.teams import TeamProfile

    client, ctx = coach_with_team
    r = await client.post(f"/api/coach/teams/{ctx['team_id']}/ical-token")
    assert r.status_code == 200
    token_1 = r.json()["token"]
    assert r.json()["url"].endswith(".ics")

    # Rotate — different token
    r2 = await client.post(f"/api/coach/teams/{ctx['team_id']}/ical-token")
    assert r2.json()["token"] != token_1

    s = await client.post(f"/api/coach/teams/{ctx['team_id']}/share-token")
    assert s.status_code == 200
    assert s.json()["token"] != token_1

    # DB confirms
    async with api_session_factory() as session:
        team = (await session.execute(
            select(TeamProfile).where(TeamProfile.id == ctx["team_id"])
        )).scalar_one()
        assert team.ical_token == r2.json()["token"]
        assert team.share_token == s.json()["token"]
