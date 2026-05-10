"""Admin task tracker schemas."""

from __future__ import annotations

from datetime import date as Date  # noqa: N812 — capitalized as a type alias

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel


class AdminTaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = ""
    status: str | None = "backlog"
    priority: str | None = "medium"
    type: str | None = "feature"
    tags_json: list = Field(default_factory=list)
    link: str | None = ""
    due_date: Date | None = None


class AdminTaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    type: str | None = None
    tags_json: list | None = None
    link: str | None = None
    due_date: Date | None = None


class AdminTaskResponse(AdminTaskCreate, ORMModel):
    id: int
    completed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AdminTaskSubtaskCreate(BaseModel):
    task_id: int
    content: str
    position: int | None = 0


class AdminTaskSubtaskUpdate(BaseModel):
    content: str | None = None
    done: bool | None = None
    position: int | None = None


class AdminTaskSubtaskResponse(ORMModel):
    id: int
    task_id: int
    content: str
    done: bool | None = None
    position: int | None = None
    created_at: str | None = None


class AdminTaskCommentCreate(BaseModel):
    task_id: int
    content: str
    author: str | None = "admin"


class AdminTaskCommentResponse(ORMModel):
    id: int
    task_id: int
    author: str | None = None
    content: str
    created_at: str | None = None


__all__ = [
    "AdminTaskCommentCreate",
    "AdminTaskCommentResponse",
    "AdminTaskCreate",
    "AdminTaskResponse",
    "AdminTaskSubtaskCreate",
    "AdminTaskSubtaskResponse",
    "AdminTaskSubtaskUpdate",
    "AdminTaskUpdate",
]
