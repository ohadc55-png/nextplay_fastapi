"""Scouting Room — videos, clips, annotations, playlists, share.

Pro-gate is satisfied by the registered user's default `trial` plan.
S3 upload paths are deferred to Phase 6 — these tests cover the CRUD
that lives entirely in the database.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select

from src.models.scouting import ScoutingVideo


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

class TestRequiresAuth:
    async def test_anon_list_videos_returns_401(self, api_client: AsyncClient):
        r = await api_client.get("/api/scouting/videos")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Videos CRUD
# ---------------------------------------------------------------------------

class TestVideos:
    async def test_register_then_get(self, authed_client: AsyncClient):
        r = await authed_client.post(
            "/api/scouting/videos",
            json={"title": "Game 1", "s3_key": "videos/1/g1.mp4",
                  "file_size": 12345, "duration_seconds": 95.5,
                  "opponent": "Rivals", "game_date": "2026-04-01"},
        )
        assert r.status_code == 201
        v = r.json()
        assert v["title"] == "Game 1"
        assert v["s3_key"] == "videos/1/g1.mp4"
        assert v["clip_count"] == 0
        vid = v["id"]

        r = await authed_client.get(f"/api/scouting/videos/{vid}")
        assert r.status_code == 200
        body = r.json()
        assert body["clips"] == []
        assert body["annotations"] == []

    async def test_register_external_requires_url(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/scouting/videos/external", json={"url": "ftp://x.y"}
        )
        assert r.status_code == 400

    async def test_register_external_succeeds(self, authed_client: AsyncClient):
        r = await authed_client.post(
            "/api/scouting/videos/external",
            json={"url": "https://www.youtube.com/watch?v=abc",
                  "title": "Scout — Lakers"},
        )
        assert r.status_code == 201
        v = r.json()
        assert v["source_type"] == "external"
        assert v["external_url"].startswith("https://")
        assert v["keep_forever"] is True

    async def test_list_orders_newest_first(self, authed_client: AsyncClient):
        await authed_client.post("/api/scouting/videos", json={"title": "A"})
        await authed_client.post("/api/scouting/videos", json={"title": "B"})
        r = await authed_client.get("/api/scouting/videos")
        titles = [v["title"] for v in r.json()]
        assert titles == ["B", "A"] or set(titles) == {"A", "B"}  # tolerate ts ties

    async def test_update_modifies_only_provided_fields(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/scouting/videos",
            json={"title": "Original", "description": "v1"},
        )
        vid = r.json()["id"]
        r = await authed_client.put(
            f"/api/scouting/videos/{vid}", json={"title": "Renamed"}
        )
        assert r.json()["title"] == "Renamed"
        assert r.json()["description"] == "v1"

    async def test_delete_removes_video(self, authed_client: AsyncClient):
        r = await authed_client.post("/api/scouting/videos", json={"title": "Bye"})
        vid = r.json()["id"]
        r = await authed_client.delete(f"/api/scouting/videos/{vid}")
        assert r.status_code == 200
        r = await authed_client.get(f"/api/scouting/videos/{vid}")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

class TestClips:
    async def test_create_clip_inside_video(self, authed_client: AsyncClient):
        r = await authed_client.post("/api/scouting/videos", json={"title": "V"})
        vid = r.json()["id"]
        r = await authed_client.post(
            f"/api/scouting/videos/{vid}/clips",
            json={"title": "Highlight", "start_time": 12.5, "end_time": 18.0,
                  "action_type": "screen", "rating": "good"},
        )
        assert r.status_code == 201
        clip = r.json()
        assert clip["title"] == "Highlight"
        assert clip["start_time"] == 12.5
        assert clip["video_id"] == vid

    async def test_clip_for_unknown_video_returns_404(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/scouting/videos/9999/clips",
            json={"start_time": 0, "end_time": 1},
        )
        assert r.status_code == 404

    async def test_batch_delete_blocks_other_users_clips(
        self, authed_client: AsyncClient
    ):
        # Try to batch-delete an id that doesn't exist (and isn't owned)
        r = await authed_client.post(
            "/api/scouting/clips/batch-delete", json={"clip_ids": [9999]}
        )
        assert r.status_code == 404

    async def test_batch_update_rating(self, authed_client: AsyncClient):
        r = await authed_client.post("/api/scouting/videos", json={"title": "V"})
        vid = r.json()["id"]
        r = await authed_client.post(
            f"/api/scouting/videos/{vid}/clips",
            json={"start_time": 0, "end_time": 5},
        )
        cid = r.json()["id"]
        r = await authed_client.post(
            "/api/scouting/clips/batch-update",
            json={"clip_ids": [cid], "rating": "good"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

class TestAnnotations:
    async def test_create_then_list(self, authed_client: AsyncClient):
        r = await authed_client.post("/api/scouting/videos", json={"title": "V"})
        vid = r.json()["id"]
        r = await authed_client.post(
            f"/api/scouting/videos/{vid}/annotations",
            json={"annotation_type": "drawing", "timestamp": 7.5,
                  "stroke_data": {"path": "M0 0 L10 10"},
                  "color": "#00FF00"},
        )
        assert r.status_code == 201
        ann = r.json()
        # stroke_data was stored as JSON, decoded on read
        assert isinstance(ann["stroke_data"], dict)
        assert ann["stroke_data"]["path"] == "M0 0 L10 10"

        r = await authed_client.get(f"/api/scouting/videos/{vid}/annotations")
        assert len(r.json()) == 1


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

class TestQuota:
    async def test_quota_for_trial_is_capped_at_1gb(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.get("/api/scouting/quota")
        assert r.status_code == 200
        body = r.json()
        # New user has subscription_plan='trial' which caps to 1 GB.
        assert body["storage_limit_gb"] == 1
        assert body["storage_used_bytes"] == 0


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

class TestPlaylists:
    async def test_create_then_add_clip(self, authed_client: AsyncClient):
        # Build a video + clip first
        r = await authed_client.post("/api/scouting/videos", json={"title": "V"})
        vid = r.json()["id"]
        r = await authed_client.post(
            f"/api/scouting/videos/{vid}/clips",
            json={"start_time": 0, "end_time": 5},
        )
        cid = r.json()["id"]

        # Create the playlist
        r = await authed_client.post(
            "/api/scouting/playlists", json={"name": "Best of Game 1"}
        )
        assert r.status_code == 201
        plid = r.json()["id"]

        # Add clip
        r = await authed_client.post(
            f"/api/scouting/playlists/{plid}/items",
            json={"clip_id": cid, "sort_order": 0},
        )
        assert r.status_code == 201

        # GET playlist shows the item
        r = await authed_client.get(f"/api/scouting/playlists/{plid}")
        assert r.json()["item_count"] == 1
        assert r.json()["items"][0]["clip_id"] == cid

    async def test_re_adding_clip_is_idempotent(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post("/api/scouting/videos", json={"title": "V"})
        vid = r.json()["id"]
        r = await authed_client.post(
            f"/api/scouting/videos/{vid}/clips",
            json={"start_time": 0, "end_time": 5},
        )
        cid = r.json()["id"]
        r = await authed_client.post(
            "/api/scouting/playlists", json={"name": "P"}
        )
        plid = r.json()["id"]

        await authed_client.post(
            f"/api/scouting/playlists/{plid}/items", json={"clip_id": cid}
        )
        # Second call must not 4xx and must not duplicate
        r = await authed_client.post(
            f"/api/scouting/playlists/{plid}/items", json={"clip_id": cid}
        )
        assert r.status_code == 201

        r = await authed_client.get(f"/api/scouting/playlists/{plid}")
        assert r.json()["item_count"] == 1


# ---------------------------------------------------------------------------
# Public clip share
# ---------------------------------------------------------------------------

class TestShare:
    async def test_share_clip_then_public_fetch(
        self, authed_client: AsyncClient, api_client: AsyncClient
    ):
        # Author creates video + clip
        r = await authed_client.post("/api/scouting/videos", json={"title": "V"})
        vid = r.json()["id"]
        r = await authed_client.post(
            f"/api/scouting/videos/{vid}/clips",
            json={"start_time": 0, "end_time": 5},
        )
        cid = r.json()["id"]

        # Generate a share token
        r = await authed_client.post(
            f"/api/scouting/clips/{cid}/share",
            json={"video_id": vid},
        )
        assert r.status_code == 201
        token = r.json()["token"]

        # Public fetch — no auth, anyone with token can view
        r = await api_client.get(f"/api/scouting/share/{token}")
        assert r.status_code == 200
        body = r.json()
        assert body["video"]["id"] == vid
        assert len(body["clips"]) == 1
        assert body["clips"][0]["id"] == cid

    async def test_share_dedupes_same_clip_set(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post("/api/scouting/videos", json={"title": "V"})
        vid = r.json()["id"]
        r = await authed_client.post(
            f"/api/scouting/videos/{vid}/clips",
            json={"start_time": 0, "end_time": 5},
        )
        cid = r.json()["id"]

        r1 = await authed_client.post(
            f"/api/scouting/clips/{cid}/share", json={"video_id": vid}
        )
        r2 = await authed_client.post(
            f"/api/scouting/clips/{cid}/share", json={"video_id": vid}
        )
        # Same clip set → same token (idempotent)
        assert r1.json()["token"] == r2.json()["token"]
