"""User repository — auth + profile lookups.

Convention: every active-user lookup filters `deleted_at IS NULL`. The
v1.0-flask code does this consistently and any caller that wants a
soft-deleted user should call `get_including_deleted(...)` explicitly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.users import User
from src.repositories.base_repository import BaseRepository


class UsersRepository(BaseRepository[User]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, User)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_active(self, user_id: int) -> User | None:
        """Active = not soft-deleted. Mirrors `auth/utils.py:179`."""
        stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_email_active(self, email: str) -> User | None:
        """Mirrors `auth/utils.py:169`."""
        stmt = select(User).where(User.email == email, User.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_unsubscribe_token(self, token: str) -> User | None:
        """Mirrors `email/preferences.py:97`. Used by `/unsubscribe` route."""
        stmt = select(User).where(User.unsubscribe_token == token)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_in_club(self, club_id: int) -> list[User]:
        """All active users in a club. For `/api/club/info`."""
        stmt = select(User).where(User.club_id == club_id, User.deleted_at.is_(None))
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_in_club(self, club_id: int) -> int:
        """Mirrors `auth/routes.py:593` (seat-cap enforcement)."""
        stmt = select(func.count()).select_from(User).where(
            User.club_id == club_id, User.deleted_at.is_(None)
        )
        return int((await self.session.execute(stmt)).scalar() or 0)

    # ------------------------------------------------------------------
    # Write — small, targeted updates that the Flask app does as one-liners.
    # Done as UPDATE statements (not load-mutate-flush) so they don't pull a
    # whole row into memory.
    # ------------------------------------------------------------------

    async def mark_logged_in(self, user_id: int) -> None:
        """Mirrors `auth/utils.py:206`."""
        stmt = update(User).where(User.id == user_id).values(last_login_at=func.now())
        await self.session.execute(stmt)
        await self.session.flush()

    async def soft_delete(self, user_id: int) -> None:
        """Mirrors `auth/utils.py:216` — soft-delete + deactivate."""
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(deleted_at=func.now(), is_active=0)
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def set_active_team(self, user_id: int, team_id: int | None) -> None:
        """Switch which team is "active" for this coach. Mirrors
        `db/__init__.py:180`. Caller must verify team ownership first."""
        stmt = update(User).where(User.id == user_id).values(active_team_id=team_id)
        await self.session.execute(stmt)
        await self.session.flush()

    async def set_subscription(
        self,
        user_id: int,
        *,
        plan: str,
        trial_ends_at: datetime | str | None = None,
    ) -> None:
        """Mirrors `auth/routes.py:224` — invite-code redeem path."""
        values: dict = {"subscription_plan": plan}
        # Flask sets trial_ends_at = NULL on redeem; mirror that semantic.
        values["trial_ends_at"] = trial_ends_at
        stmt = update(User).where(User.id == user_id).values(**values)
        await self.session.execute(stmt)
        await self.session.flush()

    async def attach_to_club(self, user_id: int, club_id: int) -> None:
        """Mirrors `auth/routes.py:615`."""
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(club_id=club_id, subscription_plan="pro")
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def detach_from_club(self, user_id: int) -> None:
        """Mirrors `auth/routes.py:663`."""
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(club_id=None, is_club_admin=0)
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def update_password(self, user_id: int, password_hash: str) -> None:
        """Mirrors `email_auth.py:238` (forgot-password) + `:293` (change-password)."""
        stmt = update(User).where(User.id == user_id).values(password_hash=password_hash)
        await self.session.execute(stmt)
        await self.session.flush()

    async def mark_email_verified(self, user_id: int) -> None:
        """Mirrors `email_auth.py:110`."""
        stmt = update(User).where(User.id == user_id).values(email_verified=1)
        await self.session.execute(stmt)
        await self.session.flush()


__all__ = ["UsersRepository"]
