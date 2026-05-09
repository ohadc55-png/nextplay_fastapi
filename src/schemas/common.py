"""Shared Pydantic types used across schema modules."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORMModel(BaseModel):
    """Base for response schemas. Sets `from_attributes=True` so they hydrate
    cleanly from SQLAlchemy ORM instances via `Schema.model_validate(orm_obj)`."""

    model_config = ConfigDict(from_attributes=True)


class PaginatedResponse(BaseModel, Generic[T]):
    """Cursor-style pagination envelope. `items` is the page; `next_cursor`
    is None when the page is the last one."""

    items: list[T]
    next_cursor: str | None = None


class StatusResponse(BaseModel):
    """Generic ack response for endpoints that don't return a domain entity."""

    status: str = "ok"
    detail: str | None = None


__all__ = ["ORMModel", "PaginatedResponse", "StatusResponse"]
