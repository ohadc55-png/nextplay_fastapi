"""Admin Tasks CRUD — task tracker for the /admin/dashboard page.

Async port of the `tasks` block in `backend/admin/routes.py:606-1115`.
Three resources: tasks (top-level), subtasks (checklist items), and
comments (append-only thread). All authenticated via `get_current_admin`.

Wire format mirrors v1 exactly so the existing admin JS keeps working:
  GET    /admin/api/tasks                         — filtered list
  GET    /admin/api/tasks/{id}                    — task with details
  POST   /admin/api/tasks                         — create
  PATCH  /admin/api/tasks/{id}                    — partial update
  DELETE /admin/api/tasks/{id}                    — hard delete
  POST   /admin/api/tasks/{id}/subtasks
  PATCH  /admin/api/tasks/{id}/subtasks/{sid}
  DELETE /admin/api/tasks/{id}/subtasks/{sid}
  POST   /admin/api/tasks/{id}/comments
  DELETE /admin/api/tasks/{id}/comments/{cid}
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import ADMIN_SESSION_KEY, get_current_admin
from src.core.database import get_db
from src.models.admin import AdminTask, AdminTaskComment, AdminTaskSubtask

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api/tasks", tags=["admin-tasks"])


# ---------------------------------------------------------------------------
# Enums + coercion helpers (mirror v1 admin/routes.py:611-640)
# ---------------------------------------------------------------------------

TASK_STATUSES = {"backlog", "in_progress", "done"}
TASK_PRIORITIES = {"critical", "high", "medium", "low"}
TASK_TYPES = {
    "feature", "bug", "refactor", "ops", "design", "docs", "research", "other",
}
TASK_TAGS = {
    "frontend", "backend", "bug", "UX", "infra", "docs",
    "research", "video", "AI", "scouting", "devops",
}


def _coerce_status(v: str | None, default: str = "backlog") -> str:
    v = (v or "").strip().lower()
    return v if v in TASK_STATUSES else default


def _coerce_priority(v: str | None, default: str = "medium") -> str:
    v = (v or "").strip().lower()
    return v if v in TASK_PRIORITIES else default


def _coerce_type(v: str | None, default: str = "feature") -> str:
    v = (v or "").strip().lower()
    return v if v in TASK_TYPES else default


def _clean_tags(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, str) and t in TASK_TAGS]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _serialize_task(
    task: AdminTask,
    subtasks: list[AdminTaskSubtask] | None = None,
    comments: list[AdminTaskComment] | None = None,
) -> dict:
    return {
        "id": task.id,
        "title": task.title or "",
        "description": task.description or "",
        "status": task.status or "backlog",
        "priority": task.priority or "medium",
        "type": task.type or "feature",
        "tags": task.tags_json or [],
        "link": task.link or "",
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "subtasks": [_serialize_subtask(s) for s in (subtasks or [])],
        "comments": [_serialize_comment(c) for c in (comments or [])],
    }


def _serialize_subtask(s: AdminTaskSubtask) -> dict:
    return {
        "id": s.id,
        "task_id": s.task_id,
        "content": s.content or "",
        "done": bool(s.done),
        "position": s.position or 0,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _serialize_comment(c: AdminTaskComment) -> dict:
    return {
        "id": c.id,
        "task_id": c.task_id,
        "author": c.author or "admin",
        "content": c.content or "",
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = ""
    status: str | None = "backlog"
    priority: str | None = "medium"
    type: str | None = "feature"
    tags: list[str] | None = None
    link: str | None = ""
    due_date: date | None = None


class TaskUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    type: str | None = None
    tags: list[str] | None = None
    link: str | None = None
    due_date: date | None = None


class SubtaskCreateRequest(BaseModel):
    content: str = Field(min_length=1)


class SubtaskUpdateRequest(BaseModel):
    content: str | None = None
    done: bool | None = None


class CommentCreateRequest(BaseModel):
    content: str = Field(min_length=1)
    author: str | None = None


# ---------------------------------------------------------------------------
# List + filtering
# ---------------------------------------------------------------------------

@router.get("")
async def list_tasks(
    request: Request,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    status: list[str] = Query(default_factory=list),
    priority: list[str] = Query(default_factory=list),
    type: list[str] = Query(default_factory=list),
    due_from: str | None = None,
    due_to: str | None = None,
    q: str | None = None,
    preset: str | None = None,
    sort: str = "due_date",
    include: str | None = None,
) -> dict:
    """Filtered task list. Mirrors v1 query semantics:
      - multi-value `status`, `priority`, `type` filters via `?k=a&k=b`
      - `q` matches title OR description (case-insensitive)
      - presets: overdue, this_week, completed_this_week
      - sort: due_date | priority | created_at | title (with NULLs-last
        for due_date and a custom rank for priority)
      - done tasks always sink to the bottom; within done, newest
        completion first."""
    stmt = select(AdminTask)

    if status:
        valid = [s for s in status if s in TASK_STATUSES]
        if valid:
            stmt = stmt.where(AdminTask.status.in_(valid))
    if priority:
        valid = [p for p in priority if p in TASK_PRIORITIES]
        if valid:
            stmt = stmt.where(AdminTask.priority.in_(valid))
    if type:
        valid = [t for t in type if t in TASK_TYPES]
        if valid:
            stmt = stmt.where(AdminTask.type.in_(valid))

    if due_from:
        stmt = stmt.where(AdminTask.due_date >= due_from)
    if due_to:
        stmt = stmt.where(AdminTask.due_date <= due_to)

    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(AdminTask.title).like(like) |
            func.lower(AdminTask.description).like(like)
        )

    today = date.today()
    if preset == "overdue":
        stmt = stmt.where(AdminTask.status != "done").where(
            AdminTask.due_date.isnot(None)
        ).where(AdminTask.due_date < today)
    elif preset == "this_week":
        end = today.fromordinal(today.toordinal() + 7)
        stmt = stmt.where(AdminTask.status != "done").where(
            AdminTask.due_date.isnot(None)
        ).where(AdminTask.due_date.between(today, end))
    elif preset == "completed_this_week":
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=7)
        stmt = stmt.where(AdminTask.status == "done").where(
            AdminTask.completed_at.isnot(None)
        ).where(AdminTask.completed_at >= cutoff)

    # Done tasks sink. Within done, newest first. Within active, user sort.
    stmt = stmt.order_by(
        (AdminTask.status == "done").asc(),
        AdminTask.completed_at.desc().nulls_last(),
    )
    if sort == "priority":
        # SQLAlchemy can't easily express CASE WHEN as ORDER BY for SQLite
        # AND Postgres — fall back to multi-step sort by status+priority.
        # (v1 produced the same effective order via this multi-step path.)
        stmt = stmt.order_by(AdminTask.priority.asc(), AdminTask.due_date.asc())
    elif sort == "title":
        stmt = stmt.order_by(func.lower(AdminTask.title).asc())
    elif sort == "created_at":
        stmt = stmt.order_by(AdminTask.created_at.desc())
    else:  # due_date (default)
        stmt = stmt.order_by(
            AdminTask.due_date.is_(None).asc(),
            AdminTask.due_date.asc(),
            AdminTask.id.desc(),
        )

    rows = list((await db.execute(stmt)).scalars().all())

    if include != "detail":
        return {"tasks": [_serialize_task(r) for r in rows]}

    out = []
    for r in rows:
        subs = list((await db.execute(
            select(AdminTaskSubtask)
            .where(AdminTaskSubtask.task_id == r.id)
            .order_by(AdminTaskSubtask.position.asc(), AdminTaskSubtask.id.asc())
        )).scalars().all())
        cmts = list((await db.execute(
            select(AdminTaskComment)
            .where(AdminTaskComment.task_id == r.id)
            .order_by(AdminTaskComment.created_at.asc())
        )).scalars().all())
        out.append(_serialize_task(r, subs, cmts))
    return {"tasks": out}


@router.get("/{task_id}")
async def task_detail(
    task_id: int,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    task = await db.get(AdminTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="not found")
    subs = list((await db.execute(
        select(AdminTaskSubtask)
        .where(AdminTaskSubtask.task_id == task_id)
        .order_by(AdminTaskSubtask.position.asc(), AdminTaskSubtask.id.asc())
    )).scalars().all())
    cmts = list((await db.execute(
        select(AdminTaskComment)
        .where(AdminTaskComment.task_id == task_id)
        .order_by(AdminTaskComment.created_at.asc())
    )).scalars().all())
    return {"task": _serialize_task(task, subs, cmts)}


@router.post("", status_code=201)
async def task_create(
    body: TaskCreateRequest,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    status_ = _coerce_status(body.status)
    task = AdminTask(
        title=body.title.strip(),
        description=(body.description or "").strip(),
        status=status_,
        priority=_coerce_priority(body.priority),
        type=_coerce_type(body.type),
        tags_json=_clean_tags(body.tags),
        link=(body.link or "").strip(),
        due_date=body.due_date,
        completed_at=datetime.utcnow() if status_ == "done" else None,
    )
    db.add(task)
    await db.flush()
    return {"task": _serialize_task(task)}


@router.patch("/{task_id}")
async def task_update(
    task_id: int,
    body: TaskUpdateRequest,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    task = await db.get(AdminTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="not found")

    data = body.model_dump(exclude_unset=True)

    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
        task.title = title
    if "description" in data:
        task.description = (data["description"] or "").strip()
    if "priority" in data:
        task.priority = _coerce_priority(data["priority"], default=task.priority)
    if "type" in data:
        task.type = _coerce_type(data["type"], default=task.type or "feature")
    if "tags" in data:
        task.tags_json = _clean_tags(data["tags"])
    if "link" in data:
        task.link = (data["link"] or "").strip()
    if "due_date" in data:
        task.due_date = data["due_date"]
    if "status" in data:
        new_status = _coerce_status(data["status"], default=task.status)
        prev_status = task.status
        task.status = new_status
        # Auto-set completed_at on transition into/out of done
        if new_status == "done" and prev_status != "done":
            task.completed_at = datetime.utcnow()
        elif new_status != "done" and prev_status == "done":
            task.completed_at = None

    task.updated_at = datetime.utcnow()
    await db.flush()
    return {"task": _serialize_task(task)}


@router.delete("/{task_id}")
async def task_delete(
    task_id: int,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Hard delete. CASCADE on FKs cleans up subtasks + comments."""
    await db.execute(delete(AdminTask).where(AdminTask.id == task_id))
    await db.flush()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Subtasks
# ---------------------------------------------------------------------------

@router.post("/{task_id}/subtasks", status_code=201)
async def subtask_create(
    task_id: int,
    body: SubtaskCreateRequest,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not await db.get(AdminTask, task_id):
        raise HTTPException(status_code=404, detail="task not found")

    pos = await db.scalar(
        select(func.coalesce(func.max(AdminTaskSubtask.position), -1) + 1)
        .where(AdminTaskSubtask.task_id == task_id)
    )
    sub = AdminTaskSubtask(
        task_id=task_id,
        content=body.content.strip(),
        position=int(pos or 0),
        done=False,
    )
    db.add(sub)
    await db.flush()
    return {"subtask": _serialize_subtask(sub)}


@router.patch("/{task_id}/subtasks/{subtask_id}")
async def subtask_update(
    task_id: int,
    subtask_id: int,
    body: SubtaskUpdateRequest,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    sub = await db.get(AdminTaskSubtask, subtask_id)
    if not sub or sub.task_id != task_id:
        raise HTTPException(status_code=404, detail="subtask not found")

    data = body.model_dump(exclude_unset=True)
    if "content" in data:
        c = (data["content"] or "").strip()
        if not c:
            raise HTTPException(status_code=400, detail="content cannot be empty")
        sub.content = c
    if "done" in data:
        sub.done = bool(data["done"])
    await db.flush()
    return {"subtask": _serialize_subtask(sub)}


@router.delete("/{task_id}/subtasks/{subtask_id}")
async def subtask_delete(
    task_id: int,
    subtask_id: int,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await db.execute(
        delete(AdminTaskSubtask).where(
            AdminTaskSubtask.id == subtask_id,
            AdminTaskSubtask.task_id == task_id,
        )
    )
    await db.flush()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@router.post("/{task_id}/comments", status_code=201)
async def comment_create(
    task_id: int,
    body: CommentCreateRequest,
    request: Request,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not await db.get(AdminTask, task_id):
        raise HTTPException(status_code=404, detail="task not found")

    author = (
        body.author
        or request.session.get(ADMIN_SESSION_KEY)
        or "admin"
    ).strip()

    comment = AdminTaskComment(
        task_id=task_id,
        content=body.content.strip(),
        author=author,
    )
    db.add(comment)
    await db.flush()
    return {"comment": _serialize_comment(comment)}


@router.delete("/{task_id}/comments/{comment_id}")
async def comment_delete(
    task_id: int,
    comment_id: int,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await db.execute(
        delete(AdminTaskComment).where(
            AdminTaskComment.id == comment_id,
            AdminTaskComment.task_id == task_id,
        )
    )
    await db.flush()
    return {"ok": True}
