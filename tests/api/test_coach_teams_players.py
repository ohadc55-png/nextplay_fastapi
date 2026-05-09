"""Coach + teams + players + composite endpoints — happy paths.

Each test signs in as a fresh coach. The composite endpoints don't have
much data on an empty DB, so we validate shape over content for those.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select

from src.models.coach import CoachPreference, Feedback
from src.models.players import Player, PlayerMetric
from src.models.teams import TeamProfile
from src.models.users import User


# ---------------------------------------------------------------------------
# Coach
# ---------------------------------------------------------------------------

class TestCoach:
    async def test_update_profile_saves_display_name(
        self, authed_client: AsyncClient, api_session_factory
    ):
        r = await authed_client.post(
            "/api/coach/profile", json={"display_name": "Coach K"}
        )
        assert r.status_code == 200
        assert r.json()["display_name"] == "Coach K"
        async with api_session_factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            assert user.display_name == "Coach K"

    async def test_save_settings_creates_then_updates(
        self, authed_client: AsyncClient, api_session_factory
    ):
        r = await authed_client.post(
            "/api/coach/settings",
            json={"detail_level": "high", "preferred_language": "en"},
        )
        assert r.status_code == 200
        assert r.json()["detail_level"] == "high"

        # Second call updates the same row
        r = await authed_client.post(
            "/api/coach/settings", json={"detail_level": "low"}
        )
        assert r.json()["detail_level"] == "low"

        async with api_session_factory() as s:
            rows = (await s.execute(select(CoachPreference))).scalars().all()
            assert len(rows) == 1  # not duplicated

    async def test_feedback_persists_with_truncation(
        self, authed_client: AsyncClient, api_session_factory
    ):
        r = await authed_client.post(
            "/api/feedback",
            json={
                "rating": 1, "comment": "good", "agent": "scout",
                "message": "x" * 600, "response": "y" * 600,
            },
        )
        assert r.status_code == 200
        async with api_session_factory() as s:
            fb = (await s.execute(select(Feedback))).scalar_one()
            assert fb.rating == 1
            assert len(fb.message_content) == 500  # truncated
            assert len(fb.response_content) == 500

    async def test_feedback_rejects_invalid_rating(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/feedback", json={"rating": 7}
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

class TestTeams:
    async def test_first_team_becomes_active(
        self, authed_client: AsyncClient, api_session_factory
    ):
        r = await authed_client.post(
            "/api/teams", json={"team_name": "First", "league": "EBL"}
        )
        assert r.status_code == 201
        tid = r.json()["id"]
        async with api_session_factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            assert user.active_team_id == tid

    async def test_switch_team(self, authed_client: AsyncClient):
        r1 = await authed_client.post("/api/teams", json={"team_name": "A"})
        r2 = await authed_client.post("/api/teams", json={"team_name": "B"})
        # Active should still be A (first)
        r = await authed_client.post(f"/api/teams/{r2.json()['id']}/switch")
        assert r.status_code == 200
        assert r.json()["active_team_id"] == r2.json()["id"]

    async def test_cannot_delete_only_team(self, authed_client: AsyncClient):
        r = await authed_client.post("/api/teams", json={"team_name": "Only"})
        tid = r.json()["id"]
        r = await authed_client.delete(f"/api/teams/{tid}")
        assert r.status_code == 400

    async def test_can_delete_when_multiple(self, authed_client: AsyncClient):
        r1 = await authed_client.post("/api/teams", json={"team_name": "A"})
        r2 = await authed_client.post("/api/teams", json={"team_name": "B"})
        r = await authed_client.delete(f"/api/teams/{r2.json()['id']}")
        assert r.status_code == 200
        assert r.json()["active_team_id"] == r1.json()["id"]

    async def test_save_team_profile(
        self, authed_client: AsyncClient
    ):
        await authed_client.post("/api/teams", json={"team_name": "Squad"})
        r = await authed_client.post(
            "/api/team-profile",
            json={"play_style": "fast break", "strengths": "speed"},
        )
        assert r.status_code == 200
        assert r.json()["play_style"] == "fast break"


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

class TestPlayers:
    async def test_add_player_requires_active_team(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post("/api/player", json={"name": "X"})
        assert r.status_code == 400  # no active team

    async def test_add_then_update_then_soft_delete(
        self, authed_client: AsyncClient, api_session_factory
    ):
        await authed_client.post("/api/teams", json={"team_name": "Squad"})
        r = await authed_client.post(
            "/api/player",
            json={"name": "Player A", "number": 7, "position": "PG"},
        )
        assert r.status_code == 201
        pid = r.json()["id"]

        r = await authed_client.put(
            f"/api/player/{pid}", json={"strengths": "fast hands"}
        )
        assert r.json()["strengths"] == "fast hands"
        assert r.json()["name"] == "Player A"  # untouched

        r = await authed_client.delete(f"/api/player/{pid}")
        assert r.status_code == 200
        async with api_session_factory() as s:
            p = (await s.execute(select(Player).where(Player.id == pid))).scalar_one()
            assert p.active is False  # soft delete

    async def test_save_player_metrics(
        self, authed_client: AsyncClient, api_session_factory
    ):
        await authed_client.post("/api/teams", json={"team_name": "Squad"})
        r = await authed_client.post("/api/player", json={"name": "P"})
        pid = r.json()["id"]

        r = await authed_client.post(
            f"/api/player/{pid}/metrics",
            json={"metrics": {"shot_chart": {"3pt": 0.4}}},
        )
        assert r.status_code == 200
        async with api_session_factory() as s:
            row = (await s.execute(
                select(PlayerMetric).where(PlayerMetric.player_id == pid)
            )).scalar_one()
            assert row.metrics_json == {"shot_chart": {"3pt": 0.4}}

    async def test_bulk_skips_empty_names(self, authed_client: AsyncClient):
        await authed_client.post("/api/teams", json={"team_name": "Squad"})
        r = await authed_client.post(
            "/api/players/bulk",
            json={"players": [{"name": "X"}, {"name": ""}, {"name": "Y", "number": 5}]},
        )
        body = r.json()
        assert body["inserted"] == 2
        assert body["skipped"] == 1


# ---------------------------------------------------------------------------
# Composite endpoints — shape checks on empty / minimal DB
# ---------------------------------------------------------------------------

class TestComposite:
    async def test_me_returns_user_and_subscription(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.get("/api/me")
        assert r.status_code == 200
        body = r.json()
        assert body["user"]["email"] == "tester@example.com"
        assert body["subscription_plan"] == "trial"
        assert body["trial_days_left"] >= 0
        assert body["teams"] == []
        assert body["active_team_id"] is None

    async def test_dashboard_with_active_team(
        self, authed_client: AsyncClient
    ):
        await authed_client.post("/api/teams", json={"team_name": "Squad"})
        r = await authed_client.get("/api/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert body["active_team_id"] is not None
        assert body["players"] == []
        assert body["sessions"] == []

    async def test_team_setup_data(self, authed_client: AsyncClient):
        await authed_client.post("/api/teams", json={"team_name": "Squad"})
        r = await authed_client.get("/api/team-setup/data")
        assert r.status_code == 200
        body = r.json()
        for k in ("profile", "players", "player_metrics", "teams", "active_team_id"):
            assert k in body

    async def test_chat_init_redirects_without_team(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.get("/api/chat/init")
        assert r.status_code == 200
        assert r.json()["redirect"] is True

    async def test_chat_init_with_team_returns_session(
        self, authed_client: AsyncClient
    ):
        await authed_client.post("/api/teams", json={"team_name": "Squad"})
        r = await authed_client.get("/api/chat/init")
        assert r.status_code == 200
        body = r.json()
        assert body.get("redirect") is not True
        assert "session_id" in body
        assert body["session_id"]
