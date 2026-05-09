"""Conversations repository (chat history).

The chat is the most-queried table in v1: messages stream in 1-by-1, history
is loaded per-session, and admin search uses LIKE across content. Tenant-
scoped on `user_id` + `team_id` per the same at-least-one-of rule.
"""

from __future__ import annotations

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.conversations import Conversation
from src.repositories.base_repository import TeamScopedRepository


class ConversationsRepository(TeamScopedRepository[Conversation]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Conversation)

    async def list_for_session(
        self, session_id: str, *, user_id: int | None, team_id: int | None
    ) -> list[Conversation]:
        """All messages in a session, in arrival order. Tenant-scoped:
        returns [] if neither user_id nor team_id is given (mirrors
        `db/__init__.py:499-527`). The session_id alone isn't enough — a
        leaked session_id from one coach must not surface another coach's
        messages."""
        if user_id is None and team_id is None:
            return []
        stmt = select(Conversation).where(Conversation.session_id == session_id)
        if user_id is not None:
            stmt = stmt.where(Conversation.user_id == user_id)
        if team_id is not None:
            stmt = stmt.where(Conversation.team_id == team_id)
        stmt = stmt.order_by(Conversation.created_at, Conversation.id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_session_ids_for_user(
        self, *, user_id: int, team_id: int | None = None, limit: int = 100
    ) -> list[str]:
        """Distinct session IDs the coach has chat history for, most-recent
        first. Used by `/api/sessions` and history page. Mirrors the
        DISTINCT ... ORDER BY MAX(created_at) shape from `db/__init__.py:538`."""
        stmt = (
            select(distinct(Conversation.session_id))
            .where(Conversation.user_id == user_id)
        )
        if team_id is not None:
            stmt = stmt.where(Conversation.team_id == team_id)
        stmt = stmt.limit(limit)
        rows = (await self.session.execute(stmt)).all()
        return [r[0] for r in rows]

    async def search(
        self,
        query: str,
        *,
        user_id: int | None,
        team_id: int | None,
        limit: int = 50,
    ) -> list[Conversation]:
        """LIKE search across content. Mirrors `db/__init__.py:640-665`.
        Returns [] if both tenant args are missing (admin search would still
        require user_id-or-team_id to scope the result)."""
        if user_id is None and team_id is None:
            return []
        like = f"%{query}%"
        stmt = select(Conversation).where(Conversation.content.ilike(like))
        if user_id is not None:
            stmt = stmt.where(Conversation.user_id == user_id)
        if team_id is not None:
            stmt = stmt.where(Conversation.team_id == team_id)
        stmt = stmt.order_by(Conversation.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["ConversationsRepository"]
