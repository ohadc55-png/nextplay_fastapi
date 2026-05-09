"""Coach Notebook repositories.

Three repos:
- NotebookEntriesRepository (TeamScoped): list by entry_type / by date /
  by player.
- NotebookAttendanceRepository: per-entry attendance roster.
- NotebookEntryPlayersRepository: M-M join between entries and players.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.notebook import NotebookAttendance, NotebookEntry, NotebookEntryPlayer
from src.repositories.base_repository import BaseRepository, TeamScopedRepository


class NotebookEntriesRepository(TeamScopedRepository[NotebookEntry]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, NotebookEntry)

    async def list_by_type(
        self, entry_type: str, *, user_id: int, team_id: int, limit: int = 100
    ) -> list[NotebookEntry]:
        """Entries of a specific type (practice_plan, game_summary, ...) for
        the active team, most-recent first."""
        stmt = (
            select(NotebookEntry)
            .where(
                NotebookEntry.user_id == user_id,
                NotebookEntry.team_id == team_id,
                NotebookEntry.entry_type == entry_type,
            )
            .order_by(NotebookEntry.entry_date.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_player(
        self, player_id: int, *, user_id: int, team_id: int
    ) -> list[NotebookEntry]:
        """Entries where this player is the legacy single-player link.
        For M-M player tagging, query NotebookEntryPlayersRepository instead."""
        stmt = (
            select(NotebookEntry)
            .where(
                NotebookEntry.user_id == user_id,
                NotebookEntry.team_id == team_id,
                NotebookEntry.player_id == player_id,
            )
            .order_by(NotebookEntry.entry_date.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())


class NotebookAttendanceRepository(BaseRepository[NotebookAttendance]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, NotebookAttendance)

    async def list_for_entry(self, entry_id: int) -> list[NotebookAttendance]:
        stmt = select(NotebookAttendance).where(NotebookAttendance.entry_id == entry_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_player(self, player_id: int) -> list[NotebookAttendance]:
        """For attendance-percentage calculation on the player profile."""
        stmt = select(NotebookAttendance).where(NotebookAttendance.player_id == player_id)
        return list((await self.session.execute(stmt)).scalars().all())


class NotebookEntryPlayersRepository(BaseRepository[NotebookEntryPlayer]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, NotebookEntryPlayer)

    async def list_player_ids_for_entry(self, entry_id: int) -> list[int]:
        stmt = (
            select(NotebookEntryPlayer.player_id)
            .where(NotebookEntryPlayer.entry_id == entry_id)
        )
        return [r[0] for r in (await self.session.execute(stmt)).all()]

    async def list_entry_ids_for_player(self, player_id: int) -> list[int]:
        stmt = (
            select(NotebookEntryPlayer.entry_id)
            .where(NotebookEntryPlayer.player_id == player_id)
        )
        return [r[0] for r in (await self.session.execute(stmt)).all()]


__all__ = [
    "NotebookEntriesRepository",
    "NotebookAttendanceRepository",
    "NotebookEntryPlayersRepository",
]
