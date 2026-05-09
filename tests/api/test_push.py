"""Smoke tests for the push router (Phase 4 batch 2).

Covers all 7 endpoints — vapid-key, subscribe, unsubscribe, preferences
(GET + POST), test, and the internal cron entrypoint. Delivery itself
(pywebpush) is stubbed; tests verify the wiring + DB state transitions.
"""

from __future__ import annotations

from unittest.mock import patch

from httpx import AsyncClient
from sqlalchemy import select

from src.core.config import settings
from src.models.push import PushSubscription
from src.models.users import User


SUB_BODY = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/test-1",
    "keys": {"p256dh": "p256-key", "auth": "auth-key"},
}


# ---------------------------------------------------------------------------
# /api/push/vapid-key — no auth required
# ---------------------------------------------------------------------------

class TestVapidKey:
    async def test_returns_empty_when_unconfigured(self, api_client: AsyncClient):
        # Default test env has no VAPID — frontend should see configured=False.
        with patch.object(settings, "VAPID_PUBLIC_KEY", ""):
            r = await api_client.get("/api/push/vapid-key")
        assert r.status_code == 200
        body = r.json()
        assert body == {"key": "", "configured": False}

    async def test_returns_key_when_configured(self, api_client: AsyncClient):
        with patch.object(settings, "VAPID_PUBLIC_KEY", "BPUBLIC..."):
            r = await api_client.get("/api/push/vapid-key")
        assert r.status_code == 200
        assert r.json() == {"key": "BPUBLIC...", "configured": True}


# ---------------------------------------------------------------------------
# /api/push/subscribe
# ---------------------------------------------------------------------------

class TestSubscribe:
    async def test_happy_path_persists_row_and_flips_enabled(
        self, authed_client: AsyncClient, api_session_factory
    ):
        r = await authed_client.post("/api/push/subscribe", json=SUB_BODY)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "push_enabled": True}

        async with api_session_factory() as s:
            subs = (await s.execute(select(PushSubscription))).scalars().all()
            assert len(subs) == 1
            assert subs[0].endpoint == SUB_BODY["endpoint"]
            assert subs[0].p256dh == "p256-key"

            user = (await s.execute(select(User))).scalar_one()
            assert user.push_enabled is True

    async def test_missing_keys_returns_400(self, authed_client: AsyncClient):
        bad = {"endpoint": "https://x", "keys": {"p256dh": "", "auth": ""}}
        r = await authed_client.post("/api/push/subscribe", json=bad)
        assert r.status_code == 400

    async def test_timezone_is_persisted_on_subscribe(
        self, authed_client: AsyncClient, api_session_factory
    ):
        body = dict(SUB_BODY)
        body["timezone"] = "America/New_York"
        r = await authed_client.post("/api/push/subscribe", json=body)
        assert r.status_code == 200

        async with api_session_factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            assert user.timezone == "America/New_York"

    async def test_invalid_timezone_is_silently_dropped(
        self, authed_client: AsyncClient, api_session_factory
    ):
        body = dict(SUB_BODY)
        body["timezone"] = "Not/A_Real_Zone"
        r = await authed_client.post("/api/push/subscribe", json=body)
        assert r.status_code == 200  # subscribe itself still succeeds

        async with api_session_factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            # Default unchanged; v1 silently rejects garbage.
            assert user.timezone == "Asia/Jerusalem"

    async def test_anonymous_request_is_rejected(self, api_client: AsyncClient):
        r = await api_client.post("/api/push/subscribe", json=SUB_BODY)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /api/push/unsubscribe
# ---------------------------------------------------------------------------

class TestUnsubscribe:
    async def test_with_endpoint_deletes_row_and_flips_off(
        self, authed_client: AsyncClient, api_session_factory
    ):
        # Subscribe first so there's a row to delete.
        await authed_client.post("/api/push/subscribe", json=SUB_BODY)

        r = await authed_client.post(
            "/api/push/unsubscribe", json={"endpoint": SUB_BODY["endpoint"]}
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "push_enabled": False}

        async with api_session_factory() as s:
            subs = (await s.execute(select(PushSubscription))).scalars().all()
            assert subs == []
            user = (await s.execute(select(User))).scalar_one()
            assert user.push_enabled is False

    async def test_empty_body_only_flips_flag_and_keeps_rows(
        self, authed_client: AsyncClient, api_session_factory
    ):
        await authed_client.post("/api/push/subscribe", json=SUB_BODY)

        r = await authed_client.post("/api/push/unsubscribe", json={})
        assert r.status_code == 200
        assert r.json()["push_enabled"] is False

        async with api_session_factory() as s:
            subs = (await s.execute(select(PushSubscription))).scalars().all()
            assert len(subs) == 1  # row still there


# ---------------------------------------------------------------------------
# /api/push/preferences
# ---------------------------------------------------------------------------

class TestPreferences:
    async def test_get_returns_defaults_for_fresh_user(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.get("/api/push/preferences")
        assert r.status_code == 200
        body = r.json()
        assert body["push_enabled"] is False
        assert body["quiet_start"] == 22
        assert body["quiet_end"] == 7
        assert body["timezone"] == "Asia/Jerusalem"

    async def test_post_quiet_hours_persists_and_echoes(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/push/preferences", json={"quiet_start": 21, "quiet_end": 6}
        )
        assert r.status_code == 200
        assert r.json() == {
            "ok": True,
            "updated": {"quiet_start": 21, "quiet_end": 6},
        }
        # Round-trip via GET
        r2 = await authed_client.get("/api/push/preferences")
        assert r2.json()["quiet_start"] == 21
        assert r2.json()["quiet_end"] == 6

    async def test_quiet_hours_out_of_range_silently_dropped(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/push/preferences", json={"quiet_start": 25, "quiet_end": 6}
        )
        assert r.status_code == 200
        assert r.json()["updated"] == {}  # nothing applied

    async def test_invalid_timezone_silently_dropped(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/push/preferences", json={"timezone": "Foo/Bar"}
        )
        assert r.status_code == 200
        assert r.json()["updated"] == {}

    async def test_only_push_enabled_flag_updates_only_that_field(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/push/preferences", json={"push_enabled": True}
        )
        assert r.status_code == 200
        assert r.json()["updated"] == {"push_enabled": True}

    async def test_anonymous_request_to_preferences_rejected(
        self, api_client: AsyncClient
    ):
        r = await api_client.get("/api/push/preferences")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /api/push/test (delivery stub until Phase 7)
# ---------------------------------------------------------------------------

class TestSelfTestPush:
    async def test_authed_user_can_fire_a_test_push(self, authed_client: AsyncClient):
        r = await authed_client.post("/api/push/test", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        # Stub flag confirms Phase 7 wiring is still pending — drop this when
        # pywebpush actually delivers.
        assert body.get("stub") is True


# ---------------------------------------------------------------------------
# /api/internal/run-push-jobs (cron, X-Cron-Secret header)
# ---------------------------------------------------------------------------

class TestCronEntry:
    async def test_503_when_secret_not_configured(self, api_client: AsyncClient):
        with patch.object(settings, "CRON_SECRET", ""):
            r = await api_client.post("/api/internal/run-push-jobs")
        assert r.status_code == 503

    async def test_403_with_wrong_secret(self, api_client: AsyncClient):
        with patch.object(settings, "CRON_SECRET", "right-secret"):
            r = await api_client.post(
                "/api/internal/run-push-jobs",
                headers={"X-Cron-Secret": "wrong"},
            )
        assert r.status_code == 403

    async def test_200_with_correct_secret(self, api_client: AsyncClient):
        with patch.object(settings, "CRON_SECRET", "right-secret"):
            r = await api_client.post(
                "/api/internal/run-push-jobs",
                headers={"X-Cron-Secret": "right-secret"},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is True
