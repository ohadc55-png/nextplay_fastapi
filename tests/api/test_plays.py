"""Play Creator router — CRUD + share token round-trip."""

from __future__ import annotations

from httpx import AsyncClient


class TestPlaysCRUD:
    async def test_anon_request_rejected(self, api_client: AsyncClient):
        r = await api_client.get("/api/plays")
        assert r.status_code == 401

    async def test_create_then_list(self, authed_client: AsyncClient):
        r = await authed_client.post(
            "/api/plays",
            json={
                "name": "Pick and Pop",
                "description": "Classic 1-5 PnR with a stretch big",
                "players": [{"id": "p1", "x": 0.5, "y": 0.5}],
                "actions": [{"type": "screen", "from": "p1", "to": "p2"}],
            },
        )
        assert r.status_code == 201
        play = r.json()["data"]
        assert play["name"] == "Pick and Pop"
        assert play["players"][0]["id"] == "p1"
        assert play["actions"][0]["type"] == "screen"

        r = await authed_client.get("/api/plays")
        assert len(r.json()["data"]) == 1

    async def test_get_returns_404_for_other_user_play(
        self, authed_client: AsyncClient, register_user, api_client: AsyncClient
    ):
        # Create play as user A
        r = await authed_client.post("/api/plays", json={"name": "Mine"})
        pid = r.json()["data"]["id"]
        # Register user B and try to fetch — different cookies, but same client
        # has user A's auth. We register B, login as B, then try.
        await register_user("other@example.com")
        # The register call sets new auth cookies, replacing user A's session.
        r = await api_client.get(f"/api/plays/{pid}")
        assert r.status_code == 404  # cross-user access blocked

    async def test_update_modifies_only_provided_fields(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/plays",
            json={"name": "Original", "description": "v1"},
        )
        pid = r.json()["data"]["id"]
        r = await authed_client.put(
            f"/api/plays/{pid}", json={"name": "Renamed"}
        )
        assert r.json()["data"]["name"] == "Renamed"
        assert r.json()["data"]["description"] == "v1"  # unchanged

    async def test_delete_removes_play(self, authed_client: AsyncClient):
        r = await authed_client.post("/api/plays", json={"name": "Bye"})
        pid = r.json()["data"]["id"]
        r = await authed_client.delete(f"/api/plays/{pid}")
        assert r.status_code == 200
        r = await authed_client.get(f"/api/plays/{pid}")
        assert r.status_code == 404


class TestPlayShare:
    async def test_share_then_fetch_publicly(
        self, authed_client: AsyncClient, api_client: AsyncClient
    ):
        # Author creates a share token
        play_data = {"name": "Shareable", "players": [], "actions": []}
        r = await authed_client.post("/api/plays/share", json=play_data)
        assert r.status_code == 201
        token = r.json()["token"]
        url = r.json()["url"]
        assert token in url

        # Public fetch — no auth, anyone can read by token
        r = await api_client.get(f"/play/{token}")
        assert r.status_code == 200
        assert r.json()["play_data"]["name"] == "Shareable"

    async def test_unknown_token_returns_404(self, api_client: AsyncClient):
        r = await api_client.get("/play/no-such-token")
        assert r.status_code == 404

    async def test_share_requires_body(self, authed_client: AsyncClient):
        r = await authed_client.post("/api/plays/share", json={})
        assert r.status_code == 400
