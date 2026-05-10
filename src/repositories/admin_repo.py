"""Admin task tracker repositories."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.admin import AdminTask, AdminTaskComment, AdminTaskSubtask
from src.repositories.base_repository import BaseRepository


class AdminTasksRepository(BaseRepository[AdminTask]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, AdminTask)

    async def list_by_status(self, status: str, *, limit: int = 200) -> list[AdminTask]:
        stmt = (
            select(AdminTask)
            .where(AdminTask.status == status)
            .order_by(AdminTask.priority.desc(), AdminTask.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_open(self, *, limit: int = 200) -> list[AdminTask]:
        """Everything that's NOT done. Powers the dashboard."""
        stmt = (
            select(AdminTask)
            .where(AdminTask.status != "done")
            .order_by(AdminTask.priority.desc(), AdminTask.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class AdminTaskSubtasksRepository(BaseRepository[AdminTaskSubtask]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, AdminTaskSubtask)

    async def list_for_task(self, task_id: int) -> list[AdminTaskSubtask]:
        stmt = (
            select(AdminTaskSubtask)
            .where(AdminTaskSubtask.task_id == task_id)
            .order_by(AdminTaskSubtask.position)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class AdminTaskCommentsRepository(BaseRepository[AdminTaskComment]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, AdminTaskComment)

    async def list_for_task(self, task_id: int) -> list[AdminTaskComment]:
        stmt = (
            select(AdminTaskComment)
            .where(AdminTaskComment.task_id == task_id)
            .order_by(AdminTaskComment.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = [
    "AdminTaskCommentsRepository",
    "AdminTaskSubtasksRepository",
    "AdminTasksRepository",
]
