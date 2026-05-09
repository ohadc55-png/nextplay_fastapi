"""Scouting + video room repositories.

Nine tables: scouting_videos, video_clips, video_annotations,
clip_playlists, playlist_items, clip_shares, storage_quota,
scouting_players, compile_cards. Most are simple CRUD over
TeamScopedRepository or BaseRepository; the standalone methods here are
the ones v1.0-flask actually queries today.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.scouting import (
    ClipPlaylist,
    ClipShare,
    CompileCard,
    PlaylistItem,
    ScoutingPlayer,
    ScoutingVideo,
    StorageQuota,
    VideoAnnotation,
    VideoClip,
)
from src.repositories.base_repository import BaseRepository, TeamScopedRepository


class ScoutingVideosRepository(TeamScopedRepository[ScoutingVideo]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, ScoutingVideo)

    async def list_unexpired(
        self, *, user_id: int, team_id: int | None
    ) -> list[ScoutingVideo]:
        """Active videos (not soft-expired) for the coach. Mirrors the
        scouting page query."""
        stmt = select(ScoutingVideo).where(ScoutingVideo.user_id == user_id)
        if team_id is not None:
            stmt = stmt.where(ScoutingVideo.team_id == team_id)
        stmt = stmt.order_by(ScoutingVideo.created_at.desc().nulls_last())
        return list((await self.session.execute(stmt)).scalars().all())


class VideoClipsRepository(BaseRepository[VideoClip]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, VideoClip)

    async def list_for_video(self, video_id: int) -> list[VideoClip]:
        stmt = (
            select(VideoClip)
            .where(VideoClip.video_id == video_id)
            .order_by(VideoClip.start_time)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class VideoAnnotationsRepository(BaseRepository[VideoAnnotation]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, VideoAnnotation)

    async def list_for_video(self, video_id: int) -> list[VideoAnnotation]:
        stmt = (
            select(VideoAnnotation)
            .where(VideoAnnotation.video_id == video_id)
            .order_by(VideoAnnotation.timestamp)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_clip(self, clip_id: int) -> list[VideoAnnotation]:
        stmt = (
            select(VideoAnnotation)
            .where(VideoAnnotation.clip_id == clip_id)
            .order_by(VideoAnnotation.timestamp)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class ClipPlaylistsRepository(TeamScopedRepository[ClipPlaylist]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, ClipPlaylist)


class PlaylistItemsRepository(BaseRepository[PlaylistItem]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, PlaylistItem)

    async def list_for_playlist(self, playlist_id: int) -> list[PlaylistItem]:
        stmt = (
            select(PlaylistItem)
            .where(PlaylistItem.playlist_id == playlist_id)
            .order_by(PlaylistItem.sort_order)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class ClipSharesRepository(BaseRepository[ClipShare]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, ClipShare)

    async def get_by_token(self, share_token: str) -> ClipShare | None:
        stmt = select(ClipShare).where(ClipShare.share_token == share_token)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class StorageQuotaRepository(BaseRepository[StorageQuota]):
    """Per-(user, team) storage quota. Adopted from prod schema (v1.0-flask
    docstring claimed singleton; prod actually has user_id + team_id)."""

    def __init__(self, session: AsyncSession):
        super().__init__(session, StorageQuota)

    async def get_for_user_team(
        self, *, user_id: int, team_id: int | None
    ) -> StorageQuota | None:
        stmt = select(StorageQuota).where(StorageQuota.user_id == user_id)
        if team_id is not None:
            stmt = stmt.where(StorageQuota.team_id == team_id)
        else:
            stmt = stmt.where(StorageQuota.team_id.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_used_bytes(
        self, *, user_id: int, team_id: int | None, delta_bytes: int
    ) -> None:
        """Increment storage_used_bytes by delta. Caller ensures the row
        exists (typically via service-layer create-on-first-upload)."""
        stmt = update(StorageQuota).where(StorageQuota.user_id == user_id)
        if team_id is not None:
            stmt = stmt.where(StorageQuota.team_id == team_id)
        else:
            stmt = stmt.where(StorageQuota.team_id.is_(None))
        stmt = stmt.values(storage_used_bytes=StorageQuota.storage_used_bytes + delta_bytes)
        await self.session.execute(stmt)
        await self.session.flush()


class ScoutingPlayersRepository(BaseRepository[ScoutingPlayer]):
    """Per-user (NOT tenant-scoped — coaches share opponent profiles across
    their teams)."""

    def __init__(self, session: AsyncSession):
        super().__init__(session, ScoutingPlayer)

    async def list_for_user(self, user_id: int) -> list[ScoutingPlayer]:
        stmt = (
            select(ScoutingPlayer)
            .where(ScoutingPlayer.user_id == user_id)
            .order_by(ScoutingPlayer.created_at.desc().nulls_last())
        )
        return list((await self.session.execute(stmt)).scalars().all())


class CompileCardsRepository(BaseRepository[CompileCard]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, CompileCard)

    async def list_for_user(
        self, user_id: int, *, card_type: str | None = None
    ) -> list[CompileCard]:
        stmt = select(CompileCard).where(CompileCard.user_id == user_id)
        if card_type is not None:
            stmt = stmt.where(CompileCard.card_type == card_type)
        stmt = stmt.order_by(CompileCard.created_at.desc().nulls_last())
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = [
    "ScoutingVideosRepository",
    "VideoClipsRepository",
    "VideoAnnotationsRepository",
    "ClipPlaylistsRepository",
    "PlaylistItemsRepository",
    "ClipSharesRepository",
    "StorageQuotaRepository",
    "ScoutingPlayersRepository",
    "CompileCardsRepository",
]
