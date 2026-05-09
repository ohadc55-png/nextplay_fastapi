"""Spot-check tests for batches 4-5 (coach + scouting + cross-cutting).

Focused on the methods with non-trivial logic (upsert, scope-respecting
lookups, rolling-window queries). Pure CRUD is left to BaseRepository
tests; per-table boilerplate doesn't need its own re-test.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.models.admin import AdminTask
from src.models.analytics import ApiUsageLog, OnboardingEvent
from src.models.coach import CoachPreference
from src.models.email import EmailLog, MailingList, MailingListMember
from src.models.push import PushLog, PushSubscription
from src.models.scouting import StorageQuota
from src.models.users import User
from src.repositories.admin_repo import AdminTasksRepository
from src.repositories.analytics_repo import ApiUsageLogsRepository, OnboardingEventsRepository
from src.repositories.coach_repo import CoachPreferencesRepository
from src.repositories.email_repo import EmailLogRepository, MailingListMembersRepository
from src.repositories.push_repo import PushLogRepository, PushSubscriptionsRepository
from src.repositories.scouting_repo import StorageQuotaRepository


async def _user(session: AsyncSession, email: str = "u@x.com") -> User:
    u = User(email=email, display_name="u")
    session.add(u)
    await session.flush()
    return u


# ---------------------------------------------------------------------------
# Coach preferences — upsert
# ---------------------------------------------------------------------------

class TestCoachPreferencesRepository:
    async def test_upsert_inserts_when_absent(self, db_session: AsyncSession):
        repo = CoachPreferencesRepository(db_session)
        u = await _user(db_session)

        await repo.upsert(user_id=u.id, preferred_language="he", detail_level="brief")

        row = await repo.get_for_user(u.id)
        assert row is not None
        assert row.preferred_language == "he"
        assert row.detail_level == "brief"

    async def test_upsert_updates_when_present(self, db_session: AsyncSession):
        repo = CoachPreferencesRepository(db_session)
        u = await _user(db_session)
        # Seed
        db_session.add(CoachPreference(user_id=u.id, preferred_language="en"))
        await db_session.flush()

        await repo.upsert(user_id=u.id, preferred_language="es")

        row = await repo.get_for_user(u.id)
        assert row is not None
        assert row.preferred_language == "es"


# ---------------------------------------------------------------------------
# Storage quota — per-(user, team) lookup with team_id IS NULL semantics
# ---------------------------------------------------------------------------

class TestStorageQuotaRepository:
    async def test_get_for_user_team_distinguishes_null_team(self, db_session: AsyncSession):
        repo = StorageQuotaRepository(db_session)
        u = await _user(db_session)
        # One row scoped to a team, one row coach-personal (team_id IS NULL)
        db_session.add(StorageQuota(user_id=u.id, team_id=42, storage_used_bytes=100))
        db_session.add(StorageQuota(user_id=u.id, team_id=None, storage_used_bytes=999))
        await db_session.flush()

        scoped = await repo.get_for_user_team(user_id=u.id, team_id=42)
        personal = await repo.get_for_user_team(user_id=u.id, team_id=None)
        assert scoped is not None and scoped.storage_used_bytes == 100
        assert personal is not None and personal.storage_used_bytes == 999


# ---------------------------------------------------------------------------
# Email log — last_sent filter
# ---------------------------------------------------------------------------

class TestEmailLogRepository:
    async def test_last_sent_filters_by_status(self, db_session: AsyncSession):
        repo = EmailLogRepository(db_session)
        u = await _user(db_session)
        # 1 sent, 1 failed — last_sent must return only the sent one.
        old_sent = datetime(2026, 1, 1)
        newer_failed = datetime(2026, 5, 1)
        db_session.add_all([
            EmailLog(user_id=u.id, to_email="u@x.com", template="trial_day_7",
                     status="sent", sent_at=old_sent),
            EmailLog(user_id=u.id, to_email="u@x.com", template="trial_day_7",
                     status="failed", sent_at=newer_failed),
        ])
        await db_session.flush()

        last = await repo.last_sent(user_id=u.id, template="trial_day_7")
        assert last is not None
        assert last.status == "sent"


# ---------------------------------------------------------------------------
# Push — has_recent_send (daily-cap rolling window)
# ---------------------------------------------------------------------------

class TestPushLogRepository:
    async def test_has_recent_send_within_window(self, db_session: AsyncSession):
        repo = PushLogRepository(db_session)
        u = await _user(db_session)
        recent = datetime.utcnow() - timedelta(hours=1)
        db_session.add(PushLog(user_id=u.id, status="sent", sent_at=recent))
        await db_session.flush()

        assert await repo.has_recent_send(user_id=u.id, hours=22) is True

    async def test_has_recent_send_outside_window(self, db_session: AsyncSession):
        repo = PushLogRepository(db_session)
        u = await _user(db_session)
        ancient = datetime.utcnow() - timedelta(days=3)
        db_session.add(PushLog(user_id=u.id, status="sent", sent_at=ancient))
        await db_session.flush()

        assert await repo.has_recent_send(user_id=u.id, hours=22) is False

    async def test_has_recent_send_excludes_failed(self, db_session: AsyncSession):
        """Failed/declined sends don't count against the daily cap."""
        repo = PushLogRepository(db_session)
        u = await _user(db_session)
        recent = datetime.utcnow() - timedelta(hours=1)
        db_session.add(PushLog(user_id=u.id, status="failed", sent_at=recent))
        await db_session.flush()

        assert await repo.has_recent_send(user_id=u.id, hours=22) is False


class TestPushSubscriptionsRepository:
    async def test_get_by_endpoint(self, db_session: AsyncSession):
        repo = PushSubscriptionsRepository(db_session)
        u = await _user(db_session)
        db_session.add(PushSubscription(
            user_id=u.id, endpoint="https://push.example/abc",
            p256dh="p256", auth="auth",
        ))
        await db_session.flush()

        found = await repo.get_by_endpoint("https://push.example/abc")
        assert found is not None
        assert await repo.get_by_endpoint("nope") is None

    async def test_delete_by_endpoint(self, db_session: AsyncSession):
        repo = PushSubscriptionsRepository(db_session)
        u = await _user(db_session)
        db_session.add(PushSubscription(
            user_id=u.id, endpoint="https://push.example/x",
            p256dh="p", auth="a",
        ))
        await db_session.flush()

        n = await repo.delete_by_endpoint("https://push.example/x")
        assert n == 1
        # Second call: nothing to delete
        assert await repo.delete_by_endpoint("https://push.example/x") == 0


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

class TestAdminTasksRepository:
    async def test_list_open_excludes_done(self, db_session: AsyncSession):
        repo = AdminTasksRepository(db_session)
        db_session.add_all([
            AdminTask(title="t1", status="backlog"),
            AdminTask(title="t2", status="in_progress"),
            AdminTask(title="t3", status="done"),
        ])
        await db_session.flush()

        rows = await repo.list_open()
        assert {t.title for t in rows} == {"t1", "t2"}


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

class TestOnboardingEventsRepository:
    async def test_has_event(self, db_session: AsyncSession):
        repo = OnboardingEventsRepository(db_session)
        u = await _user(db_session)
        db_session.add(OnboardingEvent(
            user_id=u.id, team_id=1, event="created_first_play",
            first_seen=datetime.utcnow().isoformat(),
        ))
        await db_session.flush()

        assert await repo.has_event(user_id=u.id, team_id=1, event="created_first_play") is True
        assert await repo.has_event(user_id=u.id, team_id=1, event="other") is False
        assert await repo.has_event(user_id=u.id, team_id=2, event="created_first_play") is False


class TestApiUsageLogsRepository:
    async def test_total_cost_for_user(self, db_session: AsyncSession):
        repo = ApiUsageLogsRepository(db_session)
        u_a = await _user(db_session, "a@x.com")
        u_b = await _user(db_session, "b@x.com")
        db_session.add_all([
            ApiUsageLog(user_id=u_a.id, model="gpt-4", cost_usd=0.5),
            ApiUsageLog(user_id=u_a.id, model="gpt-4", cost_usd=0.25),
            ApiUsageLog(user_id=u_b.id, model="gpt-4", cost_usd=10.0),  # other user
        ])
        await db_session.flush()

        total = await repo.total_cost_for_user(u_a.id)
        assert abs(total - 0.75) < 1e-6

    async def test_total_cost_zero_for_unknown_user(self, db_session: AsyncSession):
        repo = ApiUsageLogsRepository(db_session)
        total = await repo.total_cost_for_user(99999)
        assert total == 0.0


# ---------------------------------------------------------------------------
# Mailing lists
# ---------------------------------------------------------------------------

class TestMailingListMembersRepository:
    async def test_is_member_and_list_user_ids(self, db_session: AsyncSession):
        repo = MailingListMembersRepository(db_session)
        u_1 = await _user(db_session, "1@x.com")
        u_2 = await _user(db_session, "2@x.com")
        ml = MailingList(name="early_adopters")
        db_session.add(ml)
        await db_session.flush()
        db_session.add(MailingListMember(list_id=ml.id, user_id=u_1.id))
        db_session.add(MailingListMember(list_id=ml.id, user_id=u_2.id))
        await db_session.flush()

        assert await repo.is_member(list_id=ml.id, user_id=u_1.id) is True
        assert await repo.is_member(list_id=ml.id, user_id=99999) is False

        ids = await repo.list_user_ids_in_list(ml.id)
        assert sorted(ids) == sorted([u_1.id, u_2.id])
