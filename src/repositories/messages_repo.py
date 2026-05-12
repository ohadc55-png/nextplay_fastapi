"""Messages repository — Phase 2.5."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.messages import Message, MessageDelivery
from src.repositories.org_scoped_repository import OrgScopedRepository


class MessagesRepository(OrgScopedRepository[Message]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Message)

    async def list_for_org_ordered(
        self,
        organization_id: int | None,
        status_filter: str | None = None,
    ) -> list[Message]:
        """Newest-first list. Optional status filter (DRAFT / SENDING / SENT)."""
        if organization_id is None:
            return []
        stmt = (
            select(Message)
            .where(Message.organization_id == organization_id)
            .order_by(Message.created_at.desc())
        )
        if status_filter:
            stmt = stmt.where(Message.status == status_filter)
        return list((await self.session.execute(stmt)).scalars().all())


class MessageDeliveriesRepository(OrgScopedRepository[MessageDelivery]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, MessageDelivery)

    async def list_for_message(
        self, message_id: int, organization_id: int | None
    ) -> list[MessageDelivery]:
        if organization_id is None:
            return []
        stmt = (
            select(MessageDelivery)
            .where(MessageDelivery.message_id == message_id)
            .where(MessageDelivery.organization_id == organization_id)
            .order_by(MessageDelivery.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["MessageDeliveriesRepository", "MessagesRepository"]
