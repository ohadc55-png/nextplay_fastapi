"""Long-term memory + knowledge graph repositories.

The memory subsystem is the most semantically complex domain. The repos
here cover the SQL primitives; **semantic recall** (cosine similarity over
`embedding_json`) lives in the service layer because it's a Python-side
operation, not a SQL query.

Smart team scoping (mirrors v1.0-flask):
- Memories with `team_id IS NULL` are coach-personal (style, preference,
  philosophy) — visible across all the coach's teams.
- Memories with `team_id` set are team-specific (insight, decision,
  pattern, fact).
- The "team-scoped query" therefore reads as
  `team_id = ? OR team_id IS NULL`, not just `team_id = ?`.
"""

from __future__ import annotations

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.memory import Entity, EntityObservation, Memory, SessionSummary
from src.repositories.base_repository import BaseRepository, TeamScopedRepository


class MemoryRepository(BaseRepository[Memory]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Memory)

    async def list_for_user_team(
        self,
        user_id: int,
        team_id: int | None,
        *,
        active_only: bool = True,
        limit: int = 100,
    ) -> list[Memory]:
        """Hybrid scope: team-specific memories AND coach-personal
        (team_id IS NULL) memories. Mirrors the smart-scope read pattern in
        the v1 code that calls memory recall before chat.

        Note: this is NOT inheriting `list_for_user_team` from
        `TeamScopedRepository` because the OR-clause semantics differ —
        memories.team_id IS NULL is a meaningful state, not "no scope"."""
        stmt = select(Memory).where(Memory.user_id == user_id)
        if active_only:
            stmt = stmt.where(Memory.active.is_(True))
        if team_id is None:
            # No active team selected → only coach-personal memories.
            stmt = stmt.where(Memory.team_id.is_(None))
        else:
            stmt = stmt.where(or_(Memory.team_id == team_id, Memory.team_id.is_(None)))
        stmt = stmt.order_by(Memory.importance.desc(), Memory.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_category(
        self,
        category: str,
        *,
        user_id: int,
        team_id: int | None,
        active_only: bool = True,
    ) -> list[Memory]:
        stmt = select(Memory).where(Memory.user_id == user_id, Memory.category == category)
        if active_only:
            stmt = stmt.where(Memory.active.is_(True))
        if team_id is None:
            stmt = stmt.where(Memory.team_id.is_(None))
        else:
            stmt = stmt.where(or_(Memory.team_id == team_id, Memory.team_id.is_(None)))
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_with_embeddings(
        self,
        *,
        user_id: int,
        team_id: int | None,
        active_only: bool = True,
    ) -> list[Memory]:
        """Variant returning only rows that already have an embedding. Used
        by the semantic-recall service to skip rows that haven't been
        embedded yet."""
        stmt = select(Memory).where(
            Memory.user_id == user_id,
            Memory.embedding_json.is_not(None),
        )
        if active_only:
            stmt = stmt.where(Memory.active.is_(True))
        if team_id is None:
            stmt = stmt.where(Memory.team_id.is_(None))
        else:
            stmt = stmt.where(or_(Memory.team_id == team_id, Memory.team_id.is_(None)))
        return list((await self.session.execute(stmt)).scalars().all())

    async def mark_accessed(self, memory_id: int, *, last_accessed_at) -> None:  # noqa: ANN001
        """Bump access_count + set last_accessed_at. Caller passes the
        timestamp (typically `datetime.utcnow()`) so the service decides on
        timezone semantics. Mirrors recall-side bookkeeping in v1.0-flask."""
        stmt = (
            update(Memory)
            .where(Memory.id == memory_id)
            .values(
                access_count=Memory.access_count + 1,
                last_accessed_at=last_accessed_at,
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()


class EntityRepository(TeamScopedRepository[Entity]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Entity)

    async def get_by_name(
        self, *, entity_type: str, entity_name: str, user_id: int, team_id: int
    ) -> Entity | None:
        """Look up an entity by its UNIQUE (user_id, team_id, type, name)
        composite. Used in the upsert-on-mention flow."""
        stmt = select(Entity).where(
            Entity.user_id == user_id,
            Entity.team_id == team_id,
            Entity.entity_type == entity_type,
            Entity.entity_name == entity_name,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def bump_mention(self, entity_id: int) -> None:
        """Increment mention_count when an entity is referenced again."""
        stmt = (
            update(Entity)
            .where(Entity.id == entity_id)
            .values(mention_count=Entity.mention_count + 1)
        )
        await self.session.execute(stmt)
        await self.session.flush()


class EntityObservationsRepository(BaseRepository[EntityObservation]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, EntityObservation)

    async def list_for_entity(
        self, entity_id: int, *, limit: int = 50
    ) -> list[EntityObservation]:
        """Observations history, most-recent first."""
        stmt = (
            select(EntityObservation)
            .where(EntityObservation.entity_id == entity_id)
            .order_by(EntityObservation.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class SessionSummariesRepository(BaseRepository[SessionSummary]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, SessionSummary)

    async def get_by_session_id(self, session_id: str) -> SessionSummary | None:
        """One row per session (UNIQUE(session_id))."""
        stmt = select(SessionSummary).where(SessionSummary.session_id == session_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_recent_for_user(
        self, *, user_id: int, team_id: int | None, limit: int = 10
    ) -> list[SessionSummary]:
        """Most-recent N session summaries for prompt-context injection."""
        stmt = select(SessionSummary).where(SessionSummary.user_id == user_id)
        if team_id is not None:
            stmt = stmt.where(SessionSummary.team_id == team_id)
        stmt = stmt.order_by(SessionSummary.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = [
    "MemoryRepository",
    "EntityRepository",
    "EntityObservationsRepository",
    "SessionSummariesRepository",
]
