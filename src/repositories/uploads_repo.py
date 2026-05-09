"""Uploads repository.

Tenant-scoped. Notable methods:
- `list_for_user_team`: ordered DESC by uploaded_at (ports v1's
  `db/__init__.py:396`).
- `find_by_filename_for_user`: lookup by literal filename within a coach's
  uploads (ports `db/__init__.py:455`, used by chat-context resolution
  when a user message contains `[Uploaded: <filename>]`).
- `update_content_cache`: lazy backfill of extracted text.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.uploads import Upload
from src.repositories.base_repository import TeamScopedRepository


class UploadsRepository(TeamScopedRepository[Upload]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Upload)

    async def list_for_user_team(  # type: ignore[override]
        self,
        user_id: int | None,
        team_id: int | None,
        **_kwargs,
    ) -> list[Upload]:
        """Ordered DESC by uploaded_at. Returns [] if both args are None."""
        if user_id is None and team_id is None:
            return []
        stmt = select(Upload)
        if user_id is not None:
            stmt = stmt.where(Upload.user_id == user_id)
        if team_id is not None:
            stmt = stmt.where(Upload.team_id == team_id)
        stmt = stmt.order_by(Upload.uploaded_at.desc().nulls_last())
        return list((await self.session.execute(stmt)).scalars().all())

    async def find_by_filename_for_user(
        self, *, filename: str, user_id: int
    ) -> Upload | None:
        """Latest matching upload for a coach. Hard-requires user_id (no
        cross-tenant lookup) — mirrors `db/__init__.py:450-461`. Returns the
        most recent if a coach has multiple uploads with the same filename."""
        stmt = (
            select(Upload)
            .where(Upload.filename == filename, Upload.user_id == user_id)
            .order_by(Upload.uploaded_at.desc().nulls_last())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def update_content_cache(self, upload_id: int, content_cache: str) -> None:
        """Lazy backfill of extracted text content. Mirrors
        `db/__init__.py:467` (errors swallowed there; here we let exceptions
        propagate — service can decide to log + ignore)."""
        stmt = update(Upload).where(Upload.id == upload_id).values(content_cache=content_cache)
        await self.session.execute(stmt)
        await self.session.flush()


__all__ = ["UploadsRepository"]
