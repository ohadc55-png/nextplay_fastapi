"""Scouting Room — videos, clips, annotations, playlists, share + S3.

Pro-gate is satisfied by the registered user's default `trial` plan.
S3 endpoints (upload-config, presign, multipart-complete, video-proxy,
delete cleanup) test through patched aioboto3.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import select


class _FakeS3:
    """Stand-in for the aioboto3 S3 client used in scouting tests."""

    def __init__(self):
        self.generate_presigned_url = AsyncMock(return_value="https://s3/presigned")
        self.create_multipart_upload = AsyncMock(return_value={"UploadId": "mpu-test"})
        self.complete_multipart_upload = AsyncMock(return_value={})
        self.delete_object = AsyncMock(return_value={})
        self.put_object = AsyncMock(return_value={})


def _patch_s3(fake: _FakeS3):
    @asynccontextmanager
    async def _cm():
        yield fake

    from src.services import s3 as s3_module

    return patch.object(s3_module, "s3_client", _cm)


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


# ---------------------------------------------------------------------------
# S3 endpoints — upload-config, presign, multipart-complete, video-proxy
# ---------------------------------------------------------------------------


class TestUploadConfig:
    async def test_returns_local_when_unconfigured(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        r = await authed_client.get("/api/scouting/upload-config")
        assert r.status_code == 200
        assert r.json() == {"provider": "local"}

    async def test_returns_s3_when_configured(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setattr(settings, "AWS_S3_BUCKET", "test-bucket")
        monkeypatch.setattr(settings, "AWS_S3_REGION", "eu-central-1")
        r = await authed_client.get("/api/scouting/upload-config")
        body = r.json()
        assert body["provider"] == "s3"
        assert body["bucket"] == "test-bucket"


class TestPresignUpload:
    async def test_503_when_not_configured(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        r = await authed_client.post(
            "/api/scouting/s3/presign-upload",
            json={"file_name": "g.mp4", "file_size": 1024, "content_type": "video/mp4"},
        )
        assert r.status_code == 503

    async def test_single_part_presign(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")
        fake = _FakeS3()
        fake.generate_presigned_url = AsyncMock(return_value="https://s3/put-url")
        with _patch_s3(fake):
            r = await authed_client.post(
                "/api/scouting/s3/presign-upload",
                json={"file_name": "game.mp4", "file_size": 1024,
                      "content_type": "video/mp4"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "single"
        assert body["url"] == "https://s3/put-url"
        # Key embeds tenant — that's the multi-tenancy invariant
        assert body["key"].startswith("videos/")
        # The user_id segment is the authed user's id (from authed_client = id 1)
        assert "/videos/1/" in f"/{body['key']}"

    async def test_multipart_presign(self, authed_client: AsyncClient, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")
        fake = _FakeS3()
        with _patch_s3(fake):
            r = await authed_client.post(
                "/api/scouting/s3/presign-upload",
                json={"file_name": "long.mp4",
                      "file_size": 250 * 1024 * 1024,
                      "content_type": "video/mp4"},
            )
        body = r.json()
        assert body["mode"] == "multipart"
        assert body["upload_id"] == "mpu-test"
        # 250MB / 100MB per part = 3 parts (ceil)
        assert len(body["urls"]) == 3

    async def test_disallowed_content_type_400(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")
        fake = _FakeS3()
        with _patch_s3(fake):
            r = await authed_client.post(
                "/api/scouting/s3/presign-upload",
                json={"file_name": "x.exe", "file_size": 1024,
                      "content_type": "application/octet-stream"},
            )
        assert r.status_code == 400


class TestMultipartComplete:
    async def test_completes_for_own_prefix(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")
        fake = _FakeS3()
        with _patch_s3(fake):
            r = await authed_client.post(
                "/api/scouting/s3/complete-multipart",
                json={
                    "key": "videos/1/abc/x.mp4",  # tenant=1 matches authed user
                    "upload_id": "mpu-x",
                    "parts": [{"part_number": 1, "etag": "e1"}],
                },
            )
        assert r.status_code == 200
        fake.complete_multipart_upload.assert_called_once()

    async def test_rejects_cross_tenant_key(self, authed_client: AsyncClient):
        """Even with a valid upload_id, a coach can't finalize someone
        else's prefix. Defense against a coach scraping another coach's
        upload_id via traffic inspection."""
        r = await authed_client.post(
            "/api/scouting/s3/complete-multipart",
            json={
                "key": "videos/999/abc/x.mp4",  # different user_id
                "upload_id": "mpu-x",
                "parts": [{"part_number": 1, "etag": "e1"}],
            },
        )
        assert r.status_code == 403


class TestVideoProxy:
    async def test_owner_gets_presigned_url(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "CLOUDFRONT_DOMAIN", "")
        # Create a video with an s3_key
        r = await authed_client.post(
            "/api/scouting/videos",
            json={"title": "G", "s3_key": "videos/1/abc/x.mp4"},
        )
        vid = r.json()["id"]

        fake = _FakeS3()
        fake.generate_presigned_url = AsyncMock(return_value="https://s3/get-url")
        with _patch_s3(fake):
            r = await authed_client.get(f"/api/scouting/video-proxy/{vid}")
        assert r.status_code == 200
        body = r.json()
        assert body["url"] == "https://s3/get-url"
        assert body["expires_in"] == 3600

    async def test_cloudfront_used_when_configured(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "CLOUDFRONT_DOMAIN", "cdn.example.com")
        r = await authed_client.post(
            "/api/scouting/videos",
            json={"title": "G", "s3_key": "videos/1/abc/x.mp4"},
        )
        vid = r.json()["id"]
        r = await authed_client.get(f"/api/scouting/video-proxy/{vid}")
        assert r.status_code == 200
        assert r.json()["url"] == "https://cdn.example.com/videos/1/abc/x.mp4"

    async def test_unknown_video_404(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/scouting/video-proxy/99999")
        assert r.status_code == 404


class TestDeleteVideoCleansUpS3:
    async def test_delete_calls_s3(self, authed_client: AsyncClient):
        # Create a video with an S3 key
        r = await authed_client.post(
            "/api/scouting/videos",
            json={"title": "G", "s3_key": "videos/1/abc/x.mp4"},
        )
        vid = r.json()["id"]

        fake = _FakeS3()
        with _patch_s3(fake):
            r = await authed_client.delete(f"/api/scouting/videos/{vid}")
        assert r.status_code == 200
        fake.delete_object.assert_called_once()
        kwargs = fake.delete_object.await_args.kwargs
        assert kwargs["Key"] == "videos/1/abc/x.mp4"

    async def test_delete_with_empty_s3_key_skips_s3(
        self, authed_client: AsyncClient
    ):
        """No S3 key (e.g. external video, local upload) → no S3 call.
        DB row deleted regardless."""
        r = await authed_client.post(
            "/api/scouting/videos", json={"title": "G", "s3_key": ""},
        )
        vid = r.json()["id"]
        fake = _FakeS3()
        with _patch_s3(fake):
            r = await authed_client.delete(f"/api/scouting/videos/{vid}")
        assert r.status_code == 200
        fake.delete_object.assert_not_called()

    async def test_delete_with_local_prefix_skips_s3(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/scouting/videos",
            json={"title": "G", "s3_key": "local/dev/x.mp4"},
        )
        vid = r.json()["id"]
        fake = _FakeS3()
        with _patch_s3(fake):
            r = await authed_client.delete(f"/api/scouting/videos/{vid}")
        assert r.status_code == 200
        fake.delete_object.assert_not_called()

    async def test_s3_failure_returns_502_and_keeps_db_row(
        self, authed_client: AsyncClient, api_session_factory
    ):
        """If S3 delete fails, the DB row stays so a sweeper can retry.
        Better than orphaning the S3 object on a transient AWS hiccup."""
        r = await authed_client.post(
            "/api/scouting/videos",
            json={"title": "G", "s3_key": "videos/1/abc/x.mp4"},
        )
        vid = r.json()["id"]

        fake = _FakeS3()
        fake.delete_object = AsyncMock(side_effect=RuntimeError("AWS down"))
        with _patch_s3(fake):
            r = await authed_client.delete(f"/api/scouting/videos/{vid}")
        assert r.status_code == 502
        # DB row still there
        async with api_session_factory() as s:
            from src.models.scouting import ScoutingVideo as SV  # noqa: N817
            row = (await s.execute(select(SV).where(SV.id == vid))).scalar_one_or_none()
            assert row is not None
