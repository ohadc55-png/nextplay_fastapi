"""LLM cost-tracking helper — `calc_cost` + `log_response` round-trip.

`log_response` writes to api_usage_logs; the test verifies the row is
written with the right values. Network calls aren't made — we feed in a
fake response object that mirrors the OpenAI SDK's shape."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.crew.llm import calc_cost, log_api_usage, log_response, MODEL_PRICING
from src.models.analytics import ApiUsageLog


class TestCalcCost:
    def test_known_model(self):
        # gpt-4o-mini @ $0.15 / $0.60 per 1M tokens
        cost = calc_cost("gpt-4o-mini", 100_000, 50_000)
        # 100k * 0.15 / 1M + 50k * 0.60 / 1M = 0.015 + 0.030 = 0.045
        assert cost == pytest.approx(0.045, abs=1e-6)

    def test_unknown_model_falls_back_to_cheapest(self):
        cost = calc_cost("imaginary-model-x", 1_000_000, 0)
        # Falls back to gpt-4o-mini's input price (0.15)
        assert cost == pytest.approx(0.15, abs=1e-6)

    def test_pricing_table_includes_critical_models(self):
        # Regression: if someone removes one of these, agent costs go to 0.
        for name in ("gpt-4o", "gpt-4o-mini", "text-embedding-3-small"):
            assert name in MODEL_PRICING


class TestLogApiUsage:
    async def test_writes_row_with_calculated_cost(self, db_session):
        await log_api_usage(
            db_session,
            model="gpt-4o-mini",
            prompt_tokens=10_000, completion_tokens=5_000,
            user_id=42, team_id=7, agent_key="scout", endpoint="chat",
        )
        rows = list((await db_session.execute(select(ApiUsageLog))).scalars().all())
        assert len(rows) == 1
        row = rows[0]
        assert row.user_id == 42
        assert row.team_id == 7
        assert row.agent_key == "scout"
        assert row.endpoint == "chat"
        assert row.prompt_tokens == 10_000
        assert row.completion_tokens == 5_000
        assert row.total_tokens == 15_000
        # 10k * 0.15/1M + 5k * 0.60/1M = 0.0015 + 0.003 = 0.0045
        assert row.cost_usd == pytest.approx(0.0045, abs=1e-6)


class TestLogResponse:
    async def test_extracts_usage_from_response_object(self, db_session):
        fake_response = SimpleNamespace(
            model="gpt-4o",
            usage=SimpleNamespace(prompt_tokens=200, completion_tokens=100),
        )
        await log_response(
            db_session, fake_response,
            user_id=1, team_id=1, agent_key="brad", endpoint="chat",
        )
        rows = list((await db_session.execute(select(ApiUsageLog))).scalars().all())
        assert len(rows) == 1
        assert rows[0].model == "gpt-4o"
        assert rows[0].agent_key == "brad"
        assert rows[0].prompt_tokens == 200

    async def test_response_without_usage_is_a_noop(self, db_session):
        fake_response = SimpleNamespace(model="gpt-4o", usage=None)
        await log_response(db_session, fake_response, agent_key="x")
        rows = list((await db_session.execute(select(ApiUsageLog))).scalars().all())
        assert rows == []
