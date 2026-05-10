"""Base repository classes.

Two layers:

1. ``BaseRepository[T]`` — generic CRUD over a single SQLAlchemy model.
   `flush()` only — never commits. The caller (a service) owns the
   transaction boundary so multiple repository operations can compose
   atomically.

2. ``TeamScopedRepository[T]`` — adds tenant-aware list / get methods that
   refuse to run unless at least one of (user_id, team_id) is provided.
   This mirrors the at-least-one-of pattern from
   `backend/db/__init__.py:301-323` in v1.0-flask, encoding the
   multi-tenancy rule at the data-access layer where it can't be forgotten.

**Convention:** repositories never call services and never call other
repositories. Composition happens in the service layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.strategy_options import _AbstractLoad

from src.core.database import Base

T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    """Async CRUD over a single ORM model.

    Concrete subclasses pass `model=...` via super().__init__() and add
    domain-specific lookup methods (e.g. `get_by_email`).
    """

    def __init__(self, session: AsyncSession, model: type[T]) -> None:
        self.session = session
        self.model = model

    # --- read ---

    async def get(
        self,
        id: Any,  # noqa: A002 — matching SQLAlchemy convention
        *,
        loaders: Sequence[_AbstractLoad] = (),
    ) -> T | None:
        """Fetch by primary key. Pass `loaders=(selectinload(...),)` to
        eager-load relationships and avoid `MissingGreenlet` in async sessions.
        """
        if loaders:
            stmt = select(self.model).where(self.model.id == id)  # type: ignore[attr-defined]
            for loader in loaders:
                stmt = stmt.options(loader)
            result = await self.session.execute(stmt)
            return result.scalar_one_or_none()
        return await self.session.get(self.model, id)

    async def list_all(self, *, loaders: Sequence[_AbstractLoad] = ()) -> list[T]:
        """Return every row. Use sparingly — most queries should be filtered."""
        stmt = select(self.model)
        for loader in loaders:
            stmt = stmt.options(loader)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # --- write (flush only — no commit) ---

    async def create(self, obj: T) -> T:
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def update(self, obj: T) -> T:
        """Persist pending mutations on ``obj``. Caller must have already set
        the new attribute values; SQLAlchemy tracks them via the unit of work."""
        await self.session.flush()
        return obj

    async def delete(self, obj: T) -> None:
        await self.session.delete(obj)
        await self.session.flush()

    async def delete_by_id(self, id: Any) -> bool:  # noqa: A002
        """Single-statement DELETE WHERE id=?. Returns True if a row was hit."""
        stmt = delete(self.model).where(self.model.id == id)  # type: ignore[attr-defined]
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0


class TeamScopedRepository(BaseRepository[T]):
    """Repository for tables that carry both ``user_id`` and ``team_id``.

    Encodes the at-least-one-of(user_id, team_id) rule from
    `backend/db/__init__.py:301-323`: if both args are missing the query
    refuses to run rather than scanning the entire table. This is
    defense-in-depth against a service that forgets to thread the active team
    through. Mirror the same rule on every domain-specific lookup that adds
    further filters.
    """

    async def list_for_user_team(
        self,
        user_id: int | None,
        team_id: int | None,
        *,
        loaders: Sequence[_AbstractLoad] = (),
    ) -> list[T]:
        if user_id is None and team_id is None:
            return []
        stmt = select(self.model)
        if user_id is not None:
            stmt = stmt.where(self.model.user_id == user_id)  # type: ignore[attr-defined]
        if team_id is not None:
            stmt = stmt.where(self.model.team_id == team_id)  # type: ignore[attr-defined]
        for loader in loaders:
            stmt = stmt.options(loader)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_for_user_team(
        self,
        id: Any,  # noqa: A002
        user_id: int | None,
        team_id: int | None,
        *,
        loaders: Sequence[_AbstractLoad] = (),
    ) -> T | None:
        """PK lookup that ALSO enforces the tenant filter — returns None if the
        row exists but doesn't match the (user_id, team_id) scope. This is the
        right primitive for "get this entity if it belongs to this user/team"
        endpoints; using `get(id)` would silently leak across tenants."""
        if user_id is None and team_id is None:
            return None
        stmt = select(self.model).where(self.model.id == id)  # type: ignore[attr-defined]
        if user_id is not None:
            stmt = stmt.where(self.model.user_id == user_id)  # type: ignore[attr-defined]
        if team_id is not None:
            stmt = stmt.where(self.model.team_id == team_id)  # type: ignore[attr-defined]
        for loader in loaders:
            stmt = stmt.options(loader)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


__all__ = ["BaseRepository", "TeamScopedRepository"]
