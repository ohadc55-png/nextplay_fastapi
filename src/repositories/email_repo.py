"""Email log + mailing lists repositories."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.email import EmailLog, MailingList, MailingListMember
from src.repositories.base_repository import BaseRepository


class EmailLogRepository(BaseRepository[EmailLog]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, EmailLog)

    async def last_sent(
        self, *, user_id: int, template: str
    ) -> EmailLog | None:
        """Most recent successful send of a template to a user. Used by the
        scheduler to suppress duplicates (e.g. don't resend trial_day_7)."""
        stmt = (
            select(EmailLog)
            .where(
                EmailLog.user_id == user_id,
                EmailLog.template == template,
                EmailLog.status == "sent",
            )
            .order_by(EmailLog.sent_at.desc().nulls_last())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class MailingListsRepository(BaseRepository[MailingList]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, MailingList)

    async def get_by_name(self, name: str) -> MailingList | None:
        stmt = select(MailingList).where(MailingList.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class MailingListMembersRepository(BaseRepository[MailingListMember]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, MailingListMember)

    async def list_user_ids_in_list(self, list_id: int) -> list[int]:
        stmt = select(MailingListMember.user_id).where(MailingListMember.list_id == list_id)
        return [r[0] for r in (await self.session.execute(stmt)).all()]

    async def is_member(self, *, list_id: int, user_id: int) -> bool:
        stmt = select(MailingListMember.list_id).where(
            MailingListMember.list_id == list_id,
            MailingListMember.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None


__all__ = [
    "EmailLogRepository",
    "MailingListsRepository",
    "MailingListMembersRepository",
]
