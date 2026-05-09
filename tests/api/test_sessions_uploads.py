"""Sessions + uploads — minimal coverage of the Phase 4 surface.

Sessions is pure DB. Uploads-DELETE removes the row; KB-stats returns a
stub until Phase 5 lands ChromaDB."""

from __future__ import annotations

from datetime import datetime

from httpx import AsyncClient
from sqlalchemy import select

from src.models.conversations import Conversation
from src.models.teams import TeamProfile
from src.models.uploads import Upload
from src.models.users import User


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class TestNewSession:
    async def test_returns_a_session_id(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/new-session")
        assert r.status_code == 200
        sid = r.json()["session_id"]
        assert sid.count("_") >= 1  # YYYYMMDD_HHMMSS_<hex>

    async def test_anon_rejected(self, api_client: AsyncClient):
        r = await api_client.get("/api/new-session")
        assert r.status_code == 401


class TestSessionMessages:
    async def test_empty_session_returns_empty_list(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.get("/api/history/no-such-session")
        assert r.status_code == 200
        assert r.json() == []

    async def test_session_messages_are_tenant_scoped(
        self, authed_client: AsyncClient, api_session_factory
    ):
        # Seed a team + a conversation row for OUR user
        async with api_session_factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            t = TeamProfile(user_id=user.id, team_name="Squad")
            s.add(t)
            await s.flush()
            user.active_team_id = t.id
            s.add_all([
                Conversation(
                    session_id="s1", user_id=user.id, team_id=t.id,
                    role="user", content="hi",
                ),
                Conversation(
                    session_id="s1", user_id=user.id, team_id=t.id,
                    role="assistant", content="hello",
                ),
            ])
            await s.commit()

        r = await authed_client.get("/api/history/s1")
        assert r.status_code == 200
        assert len(r.json()) == 2
        assert [m["role"] for m in r.json()] == ["user", "assistant"]


class TestSearch:
    async def test_empty_query_returns_empty(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/search?q=")
        assert r.json() == []

    async def test_substring_match(
        self, authed_client: AsyncClient, api_session_factory
    ):
        async with api_session_factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            t = TeamProfile(user_id=user.id, team_name="Squad")
            s.add(t)
            await s.flush()
            user.active_team_id = t.id
            s.add(Conversation(
                session_id="s1", user_id=user.id, team_id=t.id,
                role="user", content="how do we beat the press defense",
            ))
            await s.commit()

        r = await authed_client.get("/api/search?q=press")
        assert len(r.json()) == 1
        assert "press" in r.json()[0]["content"].lower()


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

class TestUploadsDelete:
    async def test_delete_removes_row(
        self, authed_client: AsyncClient, api_session_factory
    ):
        async with api_session_factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            up = Upload(
                user_id=user.id, team_id=None,
                filename="report.pdf", filepath="/tmp/report.pdf",
                file_type="pdf",
            )
            s.add(up)
            await s.commit()
            uid = up.id

        r = await authed_client.delete(f"/api/upload/{uid}")
        assert r.status_code == 200

        async with api_session_factory() as s:
            row = (await s.execute(select(Upload).where(Upload.id == uid))).scalar_one_or_none()
            assert row is None

    async def test_delete_other_user_returns_404(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.delete("/api/upload/99999")
        assert r.status_code == 404


class TestKbStats:
    async def test_kb_stats_stub(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/kb-stats")
        assert r.status_code == 200
        body = r.json()
        assert body["stub"] is True
        assert body["ingested_documents"] == 0
