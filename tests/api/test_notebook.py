"""Coach Notebook — entries CRUD + game stats + match.

Each test signs in as a coach and seeds a TeamProfile + active_team_id.
"""

from __future__ import annotations

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from src.models.notebook import NotebookEntry, NotebookEntryPlayer
from src.models.players import Player, PlayerGameStat
from src.models.teams import TeamProfile
from src.models.users import User


@pytest_asyncio.fixture
async def coach_with_team(authed_client: AsyncClient, api_session_factory):
    """Seed a TeamProfile for the registered coach + flip active_team_id."""
    async with api_session_factory() as s:
        user = (await s.execute(select(User))).scalar_one()
        team = TeamProfile(user_id=user.id, team_name="Test Squad")
        s.add(team)
        await s.flush()
        user.active_team_id = team.id
        await s.commit()
        return {"client": authed_client, "user_id": user.id, "team_id": team.id}


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

class TestNotebookRequiresAuth:
    async def test_anon_list_returns_401(self, api_client: AsyncClient):
        r = await api_client.get("/api/notebook")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Entry CRUD
# ---------------------------------------------------------------------------

class TestEntries:
    async def test_no_active_team_returns_empty_list(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.get("/api/notebook")
        assert r.status_code == 200
        assert r.json() == {"success": True, "data": [], "total": 0}

    async def test_create_then_get_then_list(self, coach_with_team):
        client = coach_with_team["client"]

        r = await client.post(
            "/api/notebook",
            json={
                "entry_type": "free_document",
                "title": "Pre-game scouting note",
                "content": {"content": "Watch their PG"},
                "tags": ["scouting"],
            },
        )
        assert r.status_code == 201, r.text
        eid = r.json()["data"]["id"]

        r = await client.get(f"/api/notebook/{eid}")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["title"] == "Pre-game scouting note"
        assert d["content"] == {"content": "Watch their PG"}
        assert d["tags"] == ["scouting"]

        r = await client.get("/api/notebook")
        assert r.json()["total"] == 1

    async def test_create_with_player_ids_writes_join_rows(
        self, coach_with_team, api_session_factory
    ):
        client = coach_with_team["client"]
        async with api_session_factory() as s:
            p1 = Player(
                user_id=coach_with_team["user_id"],
                team_id=coach_with_team["team_id"],
                name="Player A", active=True,
            )
            p2 = Player(
                user_id=coach_with_team["user_id"],
                team_id=coach_with_team["team_id"],
                name="Player B", active=True,
            )
            s.add_all([p1, p2])
            await s.flush()
            pid_a, pid_b = p1.id, p2.id
            await s.commit()

        r = await client.post(
            "/api/notebook",
            json={
                "entry_type": "player_note",
                "title": "Tag two players",
                "player_ids": [pid_a, pid_b],
            },
        )
        assert r.status_code == 201
        eid = r.json()["data"]["id"]

        async with api_session_factory() as s:
            joins = (await s.execute(
                select(NotebookEntryPlayer).where(
                    NotebookEntryPlayer.entry_id == eid
                )
            )).scalars().all()
            assert {j.player_id for j in joins} == {pid_a, pid_b}

    async def test_update_changes_only_provided_fields(self, coach_with_team):
        client = coach_with_team["client"]
        r = await client.post(
            "/api/notebook",
            json={"entry_type": "free_document", "title": "Original",
                  "content": {"content": "v1"}, "tags": ["a"]},
        )
        eid = r.json()["data"]["id"]

        r = await client.put(f"/api/notebook/{eid}", json={"title": "Updated"})
        d = r.json()["data"]
        assert d["title"] == "Updated"
        assert d["content"] == {"content": "v1"}  # unchanged
        assert d["tags"] == ["a"]  # unchanged

    async def test_delete_removes_entry(self, coach_with_team):
        client = coach_with_team["client"]
        r = await client.post(
            "/api/notebook",
            json={"entry_type": "free_document", "title": "Doomed"},
        )
        eid = r.json()["data"]["id"]
        r = await client.delete(f"/api/notebook/{eid}")
        assert r.status_code == 200
        r = await client.get(f"/api/notebook/{eid}")
        assert r.status_code == 404

    async def test_filter_by_type(self, coach_with_team):
        client = coach_with_team["client"]
        await client.post(
            "/api/notebook",
            json={"entry_type": "practice_plan", "title": "P1"},
        )
        await client.post(
            "/api/notebook",
            json={"entry_type": "game_summary", "title": "G1"},
        )
        r = await client.get("/api/notebook?type=practice_plan")
        assert r.json()["total"] == 1
        assert r.json()["data"][0]["entry_type"] == "practice_plan"


# ---------------------------------------------------------------------------
# Stats + counts
# ---------------------------------------------------------------------------

class TestStats:
    async def test_empty_stats(self, coach_with_team):
        r = await coach_with_team["client"].get("/api/notebook/stats")
        assert r.status_code == 200
        assert r.json()["data"] == {"counts": {}, "total": 0}

    async def test_stats_counts_by_type(self, coach_with_team):
        client = coach_with_team["client"]
        await client.post("/api/notebook", json={"entry_type": "practice_plan", "title": "P"})
        await client.post("/api/notebook", json={"entry_type": "practice_plan", "title": "P2"})
        await client.post("/api/notebook", json={"entry_type": "game_summary", "title": "G"})
        r = await client.get("/api/notebook/stats")
        d = r.json()["data"]
        assert d["counts"]["practice_plan"] == 2
        assert d["counts"]["game_summary"] == 1
        assert d["total"] == 3


# ---------------------------------------------------------------------------
# Game stats — saving + auto-creating a game_summary entry
# ---------------------------------------------------------------------------

class TestGameStats:
    async def test_save_stats_creates_game_summary_entry_and_rows(
        self, coach_with_team, api_session_factory
    ):
        client = coach_with_team["client"]
        # Seed two players first
        async with api_session_factory() as s:
            p1 = Player(
                user_id=coach_with_team["user_id"],
                team_id=coach_with_team["team_id"],
                name="A", active=True,
            )
            p2 = Player(
                user_id=coach_with_team["user_id"],
                team_id=coach_with_team["team_id"],
                name="B", active=True,
            )
            s.add_all([p1, p2])
            await s.flush()
            pa, pb = p1.id, p2.id
            await s.commit()

        r = await client.post(
            "/api/notebook/game-stats",
            json={
                "game_date": "2026-05-01",
                "opponent": "Rivals",
                "score_us": 78, "score_them": 70,
                "players": [
                    {"player_id": pa, "name": "A", "points": 22, "ast": 5},
                    {"player_id": pb, "name": "B", "points": 14, "reb": 8},
                ],
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()["data"]
        assert body["saved"] == 2
        assert body["notebook_entry_id"] is not None

        async with api_session_factory() as s:
            stats = list((await s.execute(select(PlayerGameStat))).scalars().all())
            assert len(stats) == 2
            entry = (await s.execute(
                select(NotebookEntry).where(NotebookEntry.id == body["notebook_entry_id"])
            )).scalar_one()
            assert entry.entry_type == "game_summary"
            assert entry.content_json["score_us"] == 78
            assert entry.content_json["result"] == "W"
            assert entry.content_json["top_performer"] == "A"

    async def test_resaving_same_game_is_idempotent(
        self, coach_with_team, api_session_factory
    ):
        client = coach_with_team["client"]
        async with api_session_factory() as s:
            p = Player(
                user_id=coach_with_team["user_id"],
                team_id=coach_with_team["team_id"],
                name="A", active=True,
            )
            s.add(p)
            await s.flush()
            pid = p.id
            await s.commit()

        body = {
            "game_date": "2026-05-01", "opponent": "X",
            "players": [{"player_id": pid, "name": "A", "points": 10}],
        }
        r1 = await client.post("/api/notebook/game-stats", json=body)
        assert r1.json()["data"]["saved"] == 1
        r2 = await client.post("/api/notebook/game-stats", json=body)
        # Second call sees the row already exists → 0 saved (matches v1
        # INSERT OR IGNORE semantics).
        assert r2.json()["data"]["saved"] == 0


# ---------------------------------------------------------------------------
# Player matching
# ---------------------------------------------------------------------------

class TestPlayerMatching:
    async def test_exact_name_match_high_confidence(
        self, coach_with_team, api_session_factory
    ):
        async with api_session_factory() as s:
            s.add(Player(
                user_id=coach_with_team["user_id"],
                team_id=coach_with_team["team_id"],
                name="John Doe", number=7, active=True,
            ))
            await s.commit()

        r = await coach_with_team["client"].post(
            "/api/notebook/match-players", json={"names": ["John Doe"]}
        )
        m = r.json()["data"][0]
        assert m["matched_name"] == "John Doe"
        assert m["confidence"] == 1.0

    async def test_jersey_number_match(
        self, coach_with_team, api_session_factory
    ):
        async with api_session_factory() as s:
            s.add(Player(
                user_id=coach_with_team["user_id"],
                team_id=coach_with_team["team_id"],
                name="Jane Smith", number=23, active=True,
            ))
            await s.commit()

        r = await coach_with_team["client"].post(
            "/api/notebook/match-players", json={"names": ["#23"]}
        )
        m = r.json()["data"][0]
        assert m["matched_name"] == "Jane Smith"
        assert m["confidence"] == 0.95
