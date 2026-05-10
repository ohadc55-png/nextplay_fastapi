"""Smoke tests for the tracking router (Phase 4 batch 1).

Coverage:
  - happy path on page-views and onboarding-events
  - auth required
  - onboarding idempotency (second call with same milestone is a no-op)
  - onboarding skips silently when user has no active team
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.models.analytics import OnboardingEvent, PageView
from src.models.teams import TeamProfile
from src.models.users import User

PV_BODY = {
    "session_id": "sess-abc",
    "page_path": "/dashboard",
    "page_section": "main",
    "duration_seconds": 12,
    "entered_at": "2026-05-09T10:00:00Z",
    "exited_at": "2026-05-09T10:00:12Z",
}


class TestPageViews:
    async def test_logged_in_user_can_log_a_page_view(
        self, authed_client: AsyncClient, api_session_factory
    ):
        r = await authed_client.post("/api/tracking/page-views", json=PV_BODY)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

        async with api_session_factory() as s:
            rows = (await s.execute(select(PageView))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.session_id == "sess-abc"
        assert row.page_path == "/dashboard"
        assert row.duration_seconds == 12

    async def test_anonymous_request_is_rejected(self, api_client: AsyncClient):
        r = await api_client.post("/api/tracking/page-views", json=PV_BODY)
        assert r.status_code == 401

    async def test_missing_required_field_is_422(self, authed_client: AsyncClient):
        bad = dict(PV_BODY)
        bad.pop("page_path")
        r = await authed_client.post("/api/tracking/page-views", json=bad)
        assert r.status_code == 422


class TestOnboardingEvents:
    async def test_no_active_team_returns_200_with_explanation(
        self, authed_client: AsyncClient, api_session_factory
    ):
        # Default register flow leaves active_team_id = None.
        r = await authed_client.post(
            "/api/onboarding/events", json={"event": "created_first_play"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "no active team" in (body.get("detail") or "")

        async with api_session_factory() as s:
            rows = (await s.execute(select(OnboardingEvent))).scalars().all()
        assert rows == []

    @pytest.fixture
    async def user_with_team(self, authed_client: AsyncClient, api_session_factory):
        """Seed a team for the registered user and flip their active_team_id."""
        async with api_session_factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            team = TeamProfile(user_id=user.id, team_name="Test Squad")
            s.add(team)
            await s.flush()
            user.active_team_id = team.id
            await s.commit()
            return {"user_id": user.id, "team_id": team.id}

    async def test_first_call_writes_row(
        self, authed_client: AsyncClient, api_session_factory, user_with_team
    ):
        r = await authed_client.post(
            "/api/onboarding/events", json={"event": "uploaded_video"}
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        async with api_session_factory() as s:
            rows = (await s.execute(select(OnboardingEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].event == "uploaded_video"
        assert rows[0].user_id == user_with_team["user_id"]
        assert rows[0].team_id == user_with_team["team_id"]

    async def test_second_call_same_milestone_is_idempotent(
        self, authed_client: AsyncClient, api_session_factory, user_with_team
    ):
        body = {"event": "created_first_play"}
        r1 = await authed_client.post("/api/onboarding/events", json=body)
        assert r1.status_code == 200

        r2 = await authed_client.post("/api/onboarding/events", json=body)
        assert r2.status_code == 200
        assert r2.json().get("detail") == "already recorded"

        async with api_session_factory() as s:
            rows = (await s.execute(select(OnboardingEvent))).scalars().all()
        assert len(rows) == 1  # no duplicate row

    async def test_anonymous_request_is_rejected(self, api_client: AsyncClient):
        r = await api_client.post(
            "/api/onboarding/events", json={"event": "x"}
        )
        assert r.status_code == 401
