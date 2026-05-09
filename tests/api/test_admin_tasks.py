"""Admin Tasks CRUD — happy-path coverage of tasks/subtasks/comments.

Each test logs in as admin first (via the `admin_logged_in` fixture).
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
async def admin_logged_in(api_client: AsyncClient) -> AsyncClient:
    """Set up admin env + log in. Returns the same client with the session
    cookie persisted."""
    pw_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    with patch.object(settings, "ADMIN_PASSWORD_HASH", pw_hash), \
         patch.object(settings, "ADMIN_EMAILS", ADMIN_EMAIL):
        r = await api_client.post(
            "/admin/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        assert r.status_code == 200, r.text
        # Critical: the patches must outlive the fixture so subsequent admin
        # endpoint calls also see ADMIN_EMAILS. Yield inside the with-block.
        yield api_client


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

class TestTasksRequireAdmin:
    async def test_anon_list_returns_401(self, api_client: AsyncClient):
        r = await api_client.get("/admin/api/tasks")
        assert r.status_code == 401

    async def test_anon_create_returns_401(self, api_client: AsyncClient):
        r = await api_client.post("/admin/api/tasks", json={"title": "x"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Tasks CRUD
# ---------------------------------------------------------------------------

class TestTaskCRUD:
    async def test_create_then_get_then_list(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.post(
            "/admin/api/tasks",
            json={
                "title": "Port the chat router",
                "description": "Phase 5 chat work",
                "priority": "high",
                "type": "feature",
                "tags": ["AI", "backend"],
            },
        )
        assert r.status_code == 201, r.text
        task = r.json()["task"]
        assert task["id"] > 0
        assert task["title"] == "Port the chat router"
        assert task["priority"] == "high"
        assert task["tags"] == ["AI", "backend"]
        assert task["status"] == "backlog"

        # Detail
        r = await admin_logged_in.get(f"/admin/api/tasks/{task['id']}")
        assert r.status_code == 200
        assert r.json()["task"]["id"] == task["id"]
        assert r.json()["task"]["subtasks"] == []
        assert r.json()["task"]["comments"] == []

        # List
        r = await admin_logged_in.get("/admin/api/tasks")
        tasks = r.json()["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["id"] == task["id"]

    async def test_invalid_priority_falls_back_to_default(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.post(
            "/admin/api/tasks",
            json={"title": "X", "priority": "bogus"},
        )
        assert r.status_code == 201
        assert r.json()["task"]["priority"] == "medium"

    async def test_unknown_tags_are_silently_dropped(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.post(
            "/admin/api/tasks",
            json={"title": "X", "tags": ["AI", "not-a-real-tag"]},
        )
        assert r.json()["task"]["tags"] == ["AI"]

    async def test_patch_updates_only_provided_fields(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.post(
            "/admin/api/tasks",
            json={"title": "T1", "priority": "low"},
        )
        tid = r.json()["task"]["id"]

        r = await admin_logged_in.patch(
            f"/admin/api/tasks/{tid}",
            json={"priority": "critical"},
        )
        task = r.json()["task"]
        assert task["priority"] == "critical"
        assert task["title"] == "T1"  # unchanged

    async def test_status_done_sets_completed_at(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.post("/admin/api/tasks", json={"title": "T"})
        tid = r.json()["task"]["id"]

        r = await admin_logged_in.patch(
            f"/admin/api/tasks/{tid}", json={"status": "done"}
        )
        task = r.json()["task"]
        assert task["status"] == "done"
        assert task["completed_at"] is not None

        # Reverting clears completed_at
        r = await admin_logged_in.patch(
            f"/admin/api/tasks/{tid}", json={"status": "backlog"}
        )
        assert r.json()["task"]["completed_at"] is None

    async def test_delete_removes_task(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.post("/admin/api/tasks", json={"title": "Doomed"})
        tid = r.json()["task"]["id"]

        r = await admin_logged_in.delete(f"/admin/api/tasks/{tid}")
        assert r.status_code == 200

        r = await admin_logged_in.get(f"/admin/api/tasks/{tid}")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Subtasks
# ---------------------------------------------------------------------------

class TestSubtasks:
    async def test_create_then_toggle_done(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.post("/admin/api/tasks", json={"title": "Parent"})
        tid = r.json()["task"]["id"]

        r = await admin_logged_in.post(
            f"/admin/api/tasks/{tid}/subtasks", json={"content": "step 1"}
        )
        assert r.status_code == 201
        sid = r.json()["subtask"]["id"]
        assert r.json()["subtask"]["done"] is False

        r = await admin_logged_in.patch(
            f"/admin/api/tasks/{tid}/subtasks/{sid}", json={"done": True}
        )
        assert r.json()["subtask"]["done"] is True

    async def test_subtask_position_increments(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.post("/admin/api/tasks", json={"title": "Parent"})
        tid = r.json()["task"]["id"]

        r1 = await admin_logged_in.post(
            f"/admin/api/tasks/{tid}/subtasks", json={"content": "a"})
        r2 = await admin_logged_in.post(
            f"/admin/api/tasks/{tid}/subtasks", json={"content": "b"})
        assert r1.json()["subtask"]["position"] == 0
        assert r2.json()["subtask"]["position"] == 1

    async def test_subtask_for_unknown_task_returns_404(
        self, admin_logged_in: AsyncClient
    ):
        r = await admin_logged_in.post(
            "/admin/api/tasks/9999/subtasks", json={"content": "x"}
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

class TestComments:
    async def test_create_and_delete_comment(self, admin_logged_in: AsyncClient):
        r = await admin_logged_in.post("/admin/api/tasks", json={"title": "T"})
        tid = r.json()["task"]["id"]

        r = await admin_logged_in.post(
            f"/admin/api/tasks/{tid}/comments", json={"content": "lgtm"}
        )
        assert r.status_code == 201
        c = r.json()["comment"]
        assert c["content"] == "lgtm"
        # Author defaults to the logged-in admin email.
        assert c["author"] == ADMIN_EMAIL

        # Detail now shows the comment
        r = await admin_logged_in.get(f"/admin/api/tasks/{tid}")
        assert len(r.json()["task"]["comments"]) == 1

        # Delete
        r = await admin_logged_in.delete(
            f"/admin/api/tasks/{tid}/comments/{c['id']}"
        )
        assert r.status_code == 200

        r = await admin_logged_in.get(f"/admin/api/tasks/{tid}")
        assert r.json()["task"]["comments"] == []
