"""Admin dashboard data endpoints — smoke tests.

Most aggregations sum to zero on a fresh in-memory DB; these tests
verify each endpoint:
  - requires admin auth
  - returns the documented JSON shape
  - doesn't crash on an empty database (every count → 0)
"""

from __future__ import annotations

from unittest.mock import patch

import bcrypt
import pytest_asyncio
from httpx import AsyncClient

from src.core.config import settings


ADMIN_EMAIL = "admin@test.com"
ADMIN_PASSWORD = "AdminPass1"


@pytest_asyncio.fixture
async def admin_logged_in(api_client: AsyncClient):
    pw_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    with patch.object(settings, "ADMIN_PASSWORD_HASH", pw_hash), \
         patch.object(settings, "ADMIN_EMAILS", ADMIN_EMAIL):
        r = await api_client.post(
            "/admin/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        assert r.status_code == 200, r.text
        yield api_client


# ---------------------------------------------------------------------------
# Auth gate (sample of endpoints — all use the same dep so one is enough)
# ---------------------------------------------------------------------------

class TestRequiresAdmin:
    async def test_stats_anon_returns_401(self, api_client: AsyncClient):
        r = await api_client.get("/admin/api/stats")
        assert r.status_code == 401

    async def test_dashboard_anon_returns_401(self, api_client: AsyncClient):
        r = await api_client.get("/admin/api/dashboard")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Each endpoint returns its documented shape on an empty DB
# ---------------------------------------------------------------------------

class TestStatsEndpoint:
    async def test_returns_zero_counts_on_empty_db(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.get("/admin/api/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["total_users"] == 0
        assert body["active_7d"] == 0


class TestDashboardEndpoint:
    async def test_dashboard_summary_shape(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.get("/admin/api/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert body["total_users"] == 0
        assert body["new_7d"] == 0
        assert body["new_30d"] == 0
        assert body["task_counts"] == {
            "critical": 0, "high": 0, "medium": 0, "low": 0
        }
        assert body["open_total"] == 0
        assert body["overdue"] == 0
        assert body["task_statuses"] == ["backlog", "in_progress", "done"]

    async def test_growth_returns_30_day_series(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.get("/admin/api/dashboard/growth")
        assert r.status_code == 200
        series = r.json()["series"]
        assert len(series) == 30
        assert all(s["cnt"] == 0 for s in series)


class TestDataEndpoints:
    """Each endpoint: returns 200, has the documented top-level keys, doesn't
    crash on empty data. Detailed aggregation correctness is covered by
    focused tests once we have realistic seed data."""

    async def test_users_tab(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.get("/admin/api/users")
        assert r.status_code == 200
        body = r.json()
        for k in ("total", "new_today", "new_week", "active_7d", "plan_map", "users"):
            assert k in body

    async def test_api_costs_tab(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.get("/admin/api/api-costs")
        assert r.status_code == 200
        body = r.json()
        for k in ("cost_today", "cost_week", "cost_month", "by_model", "by_agent"):
            assert k in body

    async def test_feature_usage_tab(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.get("/admin/api/feature-usage")
        assert r.status_code == 200
        body = r.json()
        for k in ("total_chats", "total_videos", "total_plays", "adoption"):
            assert k in body

    async def test_feedback_tab(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.get("/admin/api/feedback")
        assert r.status_code == 200
        body = r.json()
        for k in ("total", "positive", "negative", "satisfaction", "per_agent"):
            assert k in body

    async def test_geography_tab(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.get("/admin/api/geography")
        assert r.status_code == 200
        body = r.json()
        for k in ("total_geolocated", "total_countries", "countries"):
            assert k in body

    async def test_user_activity_tab(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.get("/admin/api/user-activity")
        assert r.status_code == 200
        body = r.json()
        for k in ("total_time", "active_today", "avg_session", "sections", "users"):
            assert k in body

    async def test_sales_inquiries_tab(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.get("/admin/api/sales-inquiries")
        assert r.status_code == 200
        body = r.json()
        for k in ("total", "new_count", "this_week", "academy_count", "enterprise_count"):
            assert k in body

    async def test_research_sources_tab(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.get("/admin/api/research-sources")
        assert r.status_code == 200
        body = r.json()
        for k in ("total_extracts", "unique_domains", "promote_candidates", "blacklist_candidates"):
            assert k in body
