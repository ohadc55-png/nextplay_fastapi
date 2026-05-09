"""Admin task tracker models.

Powers the `/admin/tasks` page. Three tables:
- `admin_tasks`: top-level task. `tags_json` is JSON-as-TEXT.
- `admin_task_subtasks`: checklist items, sorted by `position`.
- `admin_task_comments`: append-only discussion thread.

Origin: `backend/migrations/add_admin_tasks.py`.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base, JSONText


class AdminTask(Base):
    __tablename__ = "admin_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="backlog")
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default="medium")
    type: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="feature")
    tags_json: Mapped[list | None] = mapped_column(JSONText, nullable=True, server_default="'[]'")
    link: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_admin_tasks_status", "status"),
        Index("idx_admin_tasks_priority", "priority"),
        Index("idx_admin_tasks_due_date", "due_date"),
    )


class AdminTaskSubtask(Base):
    __tablename__ = "admin_task_subtasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("admin_tasks.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    done: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=False, server_default="false"
    )
    position: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_admin_task_subtasks_task", "task_id", "position"),
    )


class AdminTaskComment(Base):
    __tablename__ = "admin_task_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("admin_tasks.id", ondelete="CASCADE"), nullable=False
    )
    author: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="admin")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_admin_task_comments_task", "task_id", "created_at"),
    )


__all__ = ["AdminTask", "AdminTaskSubtask", "AdminTaskComment"]
