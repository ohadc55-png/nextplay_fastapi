"""Coach + teams + players + composite endpoints — happy paths.

Each test signs in as a fresh coach. The composite endpoints don't have
much data on an empty DB, so we validate shape over content for those.
"""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy import select

from src.models.coach import CoachPreference, Feedback
from src.models.players import Player, PlayerMetric
from src.models.users import User


def _png_bytes(size: int = 64) -> bytes:
    """Build a real PNG of the given size in pixels — Pillow needs valid
    image bytes to decode. Cheap to produce in-test (PIL is already
    bundled for the avatar pipeline)."""
    from PIL import Image

    img = Image.new("RGB", (size, size), color=(120, 60, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeS3:
    def __init__(self):
        self.put_object = AsyncMock(return_value={})
        self.delete_object = AsyncMock(return_value={})


def _patch_s3(fake: _FakeS3):
    @asynccontextmanager
    async def _cm():
        yield fake

    from src.services import s3 as s3_module

    return patch.object(s3_module, "s3_client", _cm)


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
# Avatar — PIL crop/resize/WebP + S3 put
# ---------------------------------------------------------------------------


class TestAvatar:
    async def test_upload_returns_503_when_s3_unconfigured(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        files = {"photo": ("avatar.png", _png_bytes(), "image/png")}
        r = await authed_client.post("/api/coach/avatar", files=files)
        assert r.status_code == 503

    async def test_upload_happy_path(
        self, authed_client: AsyncClient, api_session_factory, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")

        fake = _FakeS3()
        files = {"photo": ("face.png", _png_bytes(200), "image/png")}
        with _patch_s3(fake):
            r = await authed_client.post("/api/coach/avatar", files=files)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["avatar_key"].startswith("avatars/")
        assert body["avatar_key"].endswith(".webp")

        # S3 put was called with WebP content-type
        fake.put_object.assert_called_once()
        kwargs = fake.put_object.await_args.kwargs
        assert kwargs["ContentType"] == "image/webp"
        # Body bytes are non-empty WebP-encoded data
        assert len(kwargs["Body"]) > 0

        # users.avatar_url updated to the new S3 key
        async with api_session_factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            assert user.avatar_url == body["avatar_key"]

    async def test_upload_replaces_old_avatar(
        self, authed_client: AsyncClient, api_session_factory, monkeypatch
    ):
        """Uploading a new avatar deletes the previous S3 object."""
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")

        # Seed an existing avatar URL
        async with api_session_factory() as s:
            from sqlalchemy import update
            await s.execute(
                update(User).where(User.id == 1).values(avatar_url="avatars/old-id.webp")
            )
            await s.commit()

        fake = _FakeS3()
        files = {"photo": ("new.png", _png_bytes(), "image/png")}
        with _patch_s3(fake):
            r = await authed_client.post("/api/coach/avatar", files=files)
        assert r.status_code == 200
        # Old key was deleted from S3
        fake.delete_object.assert_called_once()
        kwargs = fake.delete_object.await_args.kwargs
        assert kwargs["Key"] == "avatars/old-id.webp"

    async def test_upload_does_not_delete_oauth_avatar(
        self, authed_client: AsyncClient, api_session_factory, monkeypatch
    ):
        """OAuth avatars (https://...) are never S3-deleted — they live
        on Google/Facebook's servers."""
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")

        async with api_session_factory() as s:
            from sqlalchemy import update
            await s.execute(
                update(User).where(User.id == 1).values(
                    avatar_url="https://lh3.googleusercontent.com/abc",
                )
            )
            await s.commit()

        fake = _FakeS3()
        files = {"photo": ("new.png", _png_bytes(), "image/png")}
        with _patch_s3(fake):
            await authed_client.post("/api/coach/avatar", files=files)
        # Old OAuth URL was NOT delete-attempted
        fake.delete_object.assert_not_called()

    async def test_upload_rejects_disallowed_extension(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")
        files = {"photo": ("malware.exe", b"MZ\x00\x00", "application/octet-stream")}
        r = await authed_client.post("/api/coach/avatar", files=files)
        assert r.status_code == 400

    async def test_upload_rejects_oversized(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")
        # 10 MB > 8 MB cap
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (10 * 1024 * 1024)
        files = {"photo": ("big.png", big, "image/png")}
        r = await authed_client.post("/api/coach/avatar", files=files)
        assert r.status_code == 400

    async def test_upload_handles_corrupt_image(
        self, authed_client: AsyncClient, monkeypatch
    ):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")
        # Real PNG header but garbage body
        bad = b"\x89PNG\r\n\x1a\nthis is not a valid PNG"
        files = {"photo": ("bad.png", bad, "image/png")}
        r = await authed_client.post("/api/coach/avatar", files=files)
        assert r.status_code == 400

    async def test_delete_clears_url_and_removes_s3(
        self, authed_client: AsyncClient, api_session_factory
    ):
        # Seed a current avatar
        async with api_session_factory() as s:
            from sqlalchemy import update
            await s.execute(
                update(User).where(User.id == 1).values(avatar_url="avatars/x.webp"),
            )
            await s.commit()

        fake = _FakeS3()
        with _patch_s3(fake):
            r = await authed_client.delete("/api/coach/avatar")
        assert r.status_code == 200
        fake.delete_object.assert_called_once()
        async with api_session_factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            assert user.avatar_url is None


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
