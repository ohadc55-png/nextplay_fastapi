"""Tests for /org/api/practice/today (Phase 3)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


def _today_at(hour: int = 9) -> datetime:
    return datetime.combine(date.today(), datetime.min.time()).replace(hour=hour)


async def test_today_empty_for_fresh_org(org_admin_client: AsyncClient):
    """No practices seeded yet -> 200 with empty sessions list."""
    r = await org_admin_client.get("/org/api/practice/today")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sessions"] == []
    assert body["day"] == date.today().isoformat()
    assert body["scope"]["role"] == "org_admin"


async def test_today_returns_org_scoped_rows(
    org_admin_client: AsyncClient, api_session_factory,
):
    """An org_admin sees every practice in their org for the requested day,
    and nothing from a sibling org."""
    from src.models.organizations import Organization
    from src.models.practice_sessions import PracticeSession
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        team_a = TeamProfile(organization_id=org_id, team_name="Team A")
        s.add(team_a)
        # Sibling org with one practice — must be invisible.
        other = Organization(slug="other-org", name="Other")
        s.add(other)
        await s.flush()
        team_b = TeamProfile(organization_id=other.id, team_name="Team B")
        s.add(team_b)
        await s.flush()
        s.add_all([
            PracticeSession(
                team_id=team_a.id,
                organization_id=org_id,
                title="Practice 1",
                scheduled_at=_today_at(8),
                duration_minutes=90,
                location="Court 1",
            ),
            PracticeSession(
                team_id=team_a.id,
                organization_id=org_id,
                title="Practice 2",
                scheduled_at=_today_at(17),
                duration_minutes=60,
                location="Court 2",
            ),
            # Different org — should NOT show up
            PracticeSession(
                team_id=team_b.id,
                organization_id=other.id,
                title="Other Org Practice",
                scheduled_at=_today_at(10),
            ),
            # Same org, yesterday — out of "today" window
            PracticeSession(
                team_id=team_a.id,
                organization_id=org_id,
                title="Yesterday",
                scheduled_at=_today_at(8) - timedelta(days=1),
            ),
        ])
        await s.commit()

    r = await org_admin_client.get("/org/api/practice/today")
    body = r.json()
    titles = [row["title"] for row in body["sessions"]]
    assert titles == ["Practice 1", "Practice 2"], body
    assert all(row["organization_id"] == org_id for row in body["sessions"])


async def test_today_accepts_explicit_day_query(
    org_admin_client: AsyncClient, api_session_factory,
):
    """`?day=YYYY-MM-DD` lets the caller peek at any single day."""
    from src.models.practice_sessions import PracticeSession
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    tomorrow = date.today() + timedelta(days=1)
    async with api_session_factory() as s:
        team = TeamProfile(organization_id=org_id, team_name="Team T")
        s.add(team)
        await s.flush()
        s.add(PracticeSession(
            team_id=team.id,
            organization_id=org_id,
            title="Future Practice",
            scheduled_at=datetime.combine(tomorrow, datetime.min.time()).replace(hour=11),
        ))
        await s.commit()

    r = await org_admin_client.get(
        f"/org/api/practice/today?day={tomorrow.isoformat()}",
    )
    body = r.json()
    assert body["day"] == tomorrow.isoformat()
    assert [row["title"] for row in body["sessions"]] == ["Future Practice"]


async def test_anonymous_request_is_rejected(api_client: AsyncClient):
    """Without an org session the endpoint must NOT leak data."""
    r = await api_client.get("/org/api/practice/today")
    assert r.status_code in (401, 404)


# ---------------------------------------------------------------------------
# Phase 12 — range + create + recurring + delete
# ---------------------------------------------------------------------------


async def test_range_endpoint_returns_sessions_in_window(
    org_admin_client: AsyncClient, api_session_factory,
):
    """range[start, end) should include `start`-day sessions and exclude
    `end`-day sessions."""
    from src.models.practice_sessions import PracticeSession
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        team = TeamProfile(organization_id=org_id, team_name="Range Team")
        s.add(team)
        await s.flush()
        s.add_all([
            PracticeSession(
                team_id=team.id, organization_id=org_id,
                title="In window",
                scheduled_at=datetime(2026, 6, 5, 10, 0),
            ),
            PracticeSession(
                team_id=team.id, organization_id=org_id,
                title="Out of window",
                scheduled_at=datetime(2026, 6, 10, 10, 0),
            ),
        ])
        await s.commit()

    r = await org_admin_client.get(
        "/org/api/practice/range?start=2026-06-01&end=2026-06-08",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    titles = [x["title"] for x in body["sessions"]]
    assert titles == ["In window"]
    assert body["can_write"] is True


async def test_range_rejects_inverted_window(org_admin_client: AsyncClient):
    r = await org_admin_client.get(
        "/org/api/practice/range?start=2026-06-10&end=2026-06-01",
    )
    assert r.status_code == 422


async def test_create_practice_single_occurrence(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        team = TeamProfile(organization_id=org_id, team_name="Solo")
        s.add(team)
        await s.commit()
        team_id = team.id

    r = await org_admin_client.post(
        "/org/api/practice",
        json={
            "team_id": team_id,
            "title": "Morning",
            "scheduled_at": "2026-07-01T09:00:00",
            "duration_minutes": 60,
            "location": "Court A",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["series_id"] is None
    assert body["sessions"][0]["title"] == "Morning"


async def test_create_practice_weekly_recurrence_expands_occurrences(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Weekly recurrence creates one row per week from `scheduled_at`
    up to and including `recurrence_until`."""
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        team = TeamProfile(organization_id=org_id, team_name="Weekly Team")
        s.add(team)
        await s.commit()
        team_id = team.id

    # Wed Jul 1, repeating every Wednesday through Jul 29 → 5 occurrences.
    r = await org_admin_client.post(
        "/org/api/practice",
        json={
            "team_id": team_id,
            "title": "Practice",
            "scheduled_at": "2026-07-01T18:00:00",
            "duration_minutes": 90,
            "recurrence": "weekly",
            "recurrence_until": "2026-07-29",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["count"] == 5
    assert body["series_id"] is not None
    # Every session shares the same series_id.
    ids = {s["series_id"] for s in body["sessions"]}
    assert ids == {body["series_id"]}


async def test_create_practice_requires_recurrence_until_for_weekly(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        team = TeamProfile(organization_id=org_id, team_name="X")
        s.add(team)
        await s.commit()
        team_id = team.id

    r = await org_admin_client.post(
        "/org/api/practice",
        json={
            "team_id": team_id,
            "scheduled_at": "2026-07-01T18:00:00",
            "recurrence": "weekly",
        },
    )
    assert r.status_code == 422


async def test_coach_cannot_create_practice(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from src.models.teams import TeamProfile

    creds = await seed_org_admin(email="coach-cal@org.test", role="coach")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        team = TeamProfile(
            organization_id=org_id, team_name="Coach team",
            user_id=creds["user_id"],
        )
        s.add(team)
        await s.commit()
        team_id = team.id

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    # Coach can READ:
    r_get = await api_client.get(
        "/org/api/practice/range?start=2026-07-01&end=2026-07-08",
    )
    assert r_get.status_code == 200
    assert r_get.json()["can_write"] is False

    # Coach cannot WRITE — 404-cloak.
    r_post = await api_client.post(
        "/org/api/practice",
        json={
            "team_id": team_id,
            "scheduled_at": "2026-07-01T18:00:00",
        },
    )
    assert r_post.status_code == 404


async def test_delete_series_drops_all_future_occurrences(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Creating 5 weekly occurrences, then DELETE with ?series=true on
    the first one should drop all 5; on the middle one should drop 3."""
    from sqlalchemy import select

    from src.models.practice_sessions import PracticeSession
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        team = TeamProfile(organization_id=org_id, team_name="Series Team")
        s.add(team)
        await s.commit()
        team_id = team.id

    r = await org_admin_client.post(
        "/org/api/practice",
        json={
            "team_id": team_id,
            "scheduled_at": "2026-08-05T18:00:00",
            "recurrence": "weekly",
            "recurrence_until": "2026-09-02",
        },
    )
    sessions = r.json()["sessions"]
    assert len(sessions) == 5
    # Delete-series starting on the 3rd occurrence → drops 3 future rows.
    third_id = sessions[2]["id"]
    r_del = await org_admin_client.delete(
        "/org/api/practice/" + str(third_id) + "?series=true",
    )
    assert r_del.status_code == 200, r_del.text
    assert r_del.json()["deleted"] == 3

    async with api_session_factory() as s:
        remaining = (await s.execute(
            select(PracticeSession).where(PracticeSession.team_id == team_id)
        )).scalars().all()
        assert len(remaining) == 2


async def test_bulk_create_with_team_ids_makes_one_row_per_team(
    org_admin_client: AsyncClient, api_session_factory,
):
    """team_ids=[A, B, C] without recurrence → 3 rows."""
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        teams = [
            TeamProfile(organization_id=org_id, team_name="A"),
            TeamProfile(organization_id=org_id, team_name="B"),
            TeamProfile(organization_id=org_id, team_name="C"),
        ]
        s.add_all(teams)
        await s.commit()
        ids = [t.id for t in teams]

    r = await org_admin_client.post(
        "/org/api/practice",
        json={
            "team_ids": ids,
            "scheduled_at": "2026-09-01T18:00:00",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["team_count"] == 3
    assert body["occurrence_count"] == 1
    assert body["count"] == 3
    # Every row points at a distinct team in the list.
    returned = sorted(s["team_id"] for s in body["sessions"])
    assert returned == sorted(ids)


async def test_bulk_create_with_team_ids_and_weekly_recurrence_cross_product(
    org_admin_client: AsyncClient, api_session_factory,
):
    """2 teams × 5 weeks → 10 rows, all sharing one series_id."""
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        a = TeamProfile(organization_id=org_id, team_name="Team A")
        b = TeamProfile(organization_id=org_id, team_name="Team B")
        s.add_all([a, b])
        await s.commit()
        ids = [a.id, b.id]

    r = await org_admin_client.post(
        "/org/api/practice",
        json={
            "team_ids": ids,
            "scheduled_at": "2026-09-01T18:00:00",
            "recurrence": "weekly",
            "recurrence_until": "2026-09-29",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["team_count"] == 2
    assert body["occurrence_count"] == 5
    assert body["count"] == 10
    series_ids = {s["series_id"] for s in body["sessions"]}
    assert len(series_ids) == 1


async def test_bulk_create_rejects_team_id_and_team_ids_together(
    org_admin_client: AsyncClient, api_session_factory,
):
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        t = TeamProfile(organization_id=org_id, team_name="X")
        s.add(t)
        await s.commit()
        tid = t.id

    r = await org_admin_client.post(
        "/org/api/practice",
        json={
            "team_id": tid,
            "team_ids": [tid],
            "scheduled_at": "2026-09-01T18:00:00",
        },
    )
    assert r.status_code == 422
    assert r.json().get("code") == "ambiguous_team_target"


async def test_bulk_create_rejects_out_of_scope_team(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    """Even in bulk mode, every team_id is validated. One bad id → 404."""
    from sqlalchemy import update

    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="pm-bulk@org.test", role="program_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        a = Program(organization_id=org_id, name="A")
        b = Program(organization_id=org_id, name="B")
        s.add_all([a, b])
        await s.flush()
        r_a = Region(organization_id=org_id, name="R-A", program_id=a.id)
        r_b = Region(organization_id=org_id, name="R-B", program_id=b.id)
        s.add_all([r_a, r_b])
        await s.flush()
        team_a = TeamProfile(organization_id=org_id, region_id=r_a.id, team_name="T-A")
        team_b = TeamProfile(organization_id=org_id, region_id=r_b.id, team_name="T-B")
        s.add_all([team_a, team_b])
        await s.flush()
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "program_manager",
            )
            .values(program_id=a.id)
        )
        await s.commit()
        a_id, b_id = team_a.id, team_b.id

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    # Including team_b (outside the PM's program) → 404 cloak.
    r = await api_client.post(
        "/org/api/practice",
        json={
            "team_ids": [a_id, b_id],
            "scheduled_at": "2026-09-01T18:00:00",
        },
    )
    assert r.status_code == 404


async def test_program_manager_cannot_create_practice_outside_program(
    api_client: AsyncClient, seed_org_admin, api_session_factory,
):
    from sqlalchemy import update

    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.user_organizations import UserOrganization

    creds = await seed_org_admin(email="pm-cal@org.test", role="program_manager")
    org_id = creds["organization_id"]
    async with api_session_factory() as s:
        a = Program(organization_id=org_id, name="A")
        b = Program(organization_id=org_id, name="B")
        s.add_all([a, b])
        await s.flush()
        r_b = Region(organization_id=org_id, name="R-B", program_id=b.id)
        s.add(r_b)
        await s.flush()
        team_in_b = TeamProfile(
            organization_id=org_id, region_id=r_b.id, team_name="T-B",
        )
        s.add(team_in_b)
        await s.flush()
        await s.execute(
            update(UserOrganization)
            .where(
                UserOrganization.user_id == creds["user_id"],
                UserOrganization.role == "program_manager",
            )
            .values(program_id=a.id)
        )
        await s.commit()
        team_id = team_in_b.id

    await api_client.post(
        "/org/login", json={"email": creds["email"], "password": creds["password"]}
    )
    r = await api_client.post(
        "/org/api/practice",
        json={
            "team_id": team_id,
            "scheduled_at": "2026-07-01T18:00:00",
        },
    )
    assert r.status_code == 404
