"""Serper search wrapper — graceful fallbacks + concurrent batch."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.research import serper as serper_module


class _FakeResponse:
    def __init__(self, ok: bool = True, status: int = 200, payload: dict | None = None):
        self.ok = ok
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class TestSerperSync:
    def test_no_api_key_returns_empty(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "")
        rows = serper_module._serper_sync("scout maccabi")
        assert rows == []

    def test_empty_query_returns_empty(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "test-key")
        rows = serper_module._serper_sync("")
        assert rows == []
        rows = serper_module._serper_sync("   ")
        assert rows == []

    def test_happy_path(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "test-key")
        fake = _FakeResponse(payload={
            "organic": [
                {"title": "Maccabi Tel Aviv stats",
                 "snippet": "Won 5 of last 10",
                 "link": "https://www.basketnews.com/maccabi"},
                {"title": "EuroLeague",
                 "snippet": "table",
                 "link": "https://www.euroleaguebasketball.net/maccabi"},
            ],
        })
        with patch("requests.post", return_value=fake) as mock:
            rows = serper_module._serper_sync("scout maccabi")
        assert len(rows) == 2
        assert rows[0]["link"].startswith("https://")
        # Verify the API key + body shape
        call_kwargs = mock.call_args.kwargs
        assert call_kwargs["headers"]["X-API-KEY"] == "test-key"
        assert call_kwargs["json"]["q"] == "scout maccabi"

    def test_non_200_returns_empty(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "test-key")
        fake = _FakeResponse(ok=False, status=429)
        with patch("requests.post", return_value=fake):
            rows = serper_module._serper_sync("anything")
        assert rows == []

    def test_http_error_returns_empty(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "test-key")
        with patch("requests.post", side_effect=RuntimeError("network down")):
            rows = serper_module._serper_sync("anything")
        assert rows == []

    def test_bad_json_returns_empty(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "test-key")
        bad = SimpleNamespace(
            ok=True, status_code=200,
            json=lambda: (_ for _ in ()).throw(ValueError("not json")),
        )
        with patch("requests.post", return_value=bad):
            rows = serper_module._serper_sync("anything")
        assert rows == []


class TestSerperBatch:
    async def test_empty_query_list(self):
        rows = await serper_module.serper_batch([])
        assert rows == []

    async def test_aggregates_per_query_with_origin(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "test-key")

        def _fake_post(url, headers, json, timeout):
            q = json["q"]
            return _FakeResponse(payload={"organic": [
                {"title": f"r1 for {q}", "snippet": "s", "link": f"https://x.com/{q}"},
            ]})

        with patch("requests.post", side_effect=_fake_post):
            rows = await serper_module.serper_batch(["q1", "q2", "q3"])
        assert len(rows) == 3
        # Each row tagged with which query produced it
        origins = {r["query_origin"] for r in rows}
        assert origins == {"q1", "q2", "q3"}

    async def test_one_failing_query_doesnt_kill_batch(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "SERPER_API_KEY", "test-key")
        ok = _FakeResponse(payload={"organic": [
            {"title": "good", "snippet": "s", "link": "https://x.com/g"},
        ]})

        call_count = {"n": 0}

        def _post(url, headers, json, timeout):
            call_count["n"] += 1
            if call_count["n"] == 2:
                return _FakeResponse(ok=False, status=500)
            return ok

        with patch("requests.post", side_effect=_post):
            rows = await serper_module.serper_batch(["q1", "q2", "q3"])
        # 2 successful queries × 1 row each
        assert len(rows) == 2
