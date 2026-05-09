"""Clubs + invite codes.

- `ClubsRepository`: standard CRUD; clubs are only managed by admin so the
  custom methods are minimal.
- `InviteCodesRepository`: lookup-by-code, mark-redeemed (with optimistic
  concurrency to make double-redeem impossible).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.clubs import Club, InviteCode
from src.repositories.base_repository import BaseRepository


class ClubsRepository(BaseRepository[Club]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Club)


class InviteCodesRepository(BaseRepository[InviteCode]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, InviteCode)

    async def get_unredeemed_by_code(self, code: str) -> InviteCode | None:
        """Find an invite code that hasn't been redeemed. Returning None
        covers: code doesn't exist OR has already been used."""
        stmt = select(InviteCode).where(
            InviteCode.code == code,
            InviteCode.redeemed_by.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_redeemed(self, *, code_id: int, user_id: int) -> bool:
        """Atomically mark a code as redeemed. Uses a guarded UPDATE so two
        concurrent redeems can't both succeed — only the first wins.

        Returns True if THIS call performed the redeem, False if the row was
        already consumed by someone else."""
        stmt = (
            update(InviteCode)
            .where(InviteCode.id == code_id, InviteCode.redeemed_by.is_(None))
            .values(redeemed_by=user_id, redeemed_at=datetime.utcnow().isoformat())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def count_redeemed_for_club(self, club_id: int) -> int:
        stmt = select(func.count()).select_from(InviteCode).where(
            InviteCode.club_id == club_id,
            InviteCode.redeemed_by.is_not(None),
        )
        return int((await self.session.execute(stmt)).scalar() or 0)


__all__ = ["ClubsRepository", "InviteCodesRepository"]
