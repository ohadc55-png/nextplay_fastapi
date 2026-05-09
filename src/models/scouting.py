"""Scouting + video room models.

The scouting subsystem is the most table-heavy domain (9 tables):

- `scouting_videos`: tenant-scoped video files (S3 or external URLs).
- `video_clips`: time-range cuts within a video.
- `video_annotations`: drawing/text overlays at a timestamp.
- `clip_playlists`: tenant-scoped containers for clips.
- `playlist_items`: playlist ↔ clip M-M with sort_order.
- `clip_shares`: public share-token snapshots.
- `storage_quota`: global singleton (`id=1`); tracks total storage usage and
  default TTL. Anti-pattern preserved 1:1 from v1.0-flask — see
  MIGRATION_TODO.
- `scouting_players`: per-user opponent player intel (not tenant-scoped;
  coaches share opponent profiles across teams).
- `compile_cards`: per-user compilations (player card, opponent card,
  game plan card, ...). `config_json` is JSON-as-TEXT.

Origin: `backend/scouting/__init__.py` + `add_compile_cards.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base, JSONText


class ScoutingVideo(Base):
    __tablename__ = "scouting_videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    video_type: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="game")

    # Storage
    s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    s3_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")  # REAL state in prod is INTEGER
    duration_seconds: Mapped[float | None] = mapped_column(Float(precision=24), nullable=True, server_default="0")  # REAL in prod

    # Game metadata
    opponent: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    game_date: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")

    # Lifecycle
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    keep_forever: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    source_type: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="s3")
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_scouting_videos_user_id", "user_id"),
    )


class VideoClip(Base):
    __tablename__ = "video_clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scouting_videos.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[float] = mapped_column(Float(precision=24), nullable=False)  # REAL in prod
    end_time: Mapped[float] = mapped_column(Float(precision=24), nullable=False)  # REAL in prod
    action_type: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="other")
    rating: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class VideoAnnotation(Base):
    __tablename__ = "video_annotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scouting_videos.id", ondelete="CASCADE"), nullable=False
    )
    clip_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("video_clips.id", ondelete="SET NULL"), nullable=True
    )
    annotation_type: Mapped[str] = mapped_column(Text, nullable=False)  # drawing | text | arrow | highlight
    timestamp: Mapped[float] = mapped_column(Float(precision=24), nullable=False)  # REAL in prod
    duration: Mapped[float | None] = mapped_column(Float(precision=24), nullable=True, server_default="3.0")  # REAL in prod
    stroke_data: Mapped[str | None] = mapped_column(Text, nullable=True)  # raw SVG / canvas commands
    color: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="#FF0000")
    stroke_width: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="3")
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class ClipPlaylist(Base):
    __tablename__ = "clip_playlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class PlaylistItem(Base):
    __tablename__ = "playlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    playlist_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clip_playlists.id", ondelete="CASCADE"), nullable=False
    )
    clip_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("video_clips.id", ondelete="CASCADE"), nullable=False
    )
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    note: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")

    __table_args__ = (
        UniqueConstraint("playlist_id", "clip_id", name="uq_playlist_items_playlist_clip"),
    )


class ClipShare(Base):
    __tablename__ = "clip_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    share_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    video_id: Mapped[int] = mapped_column(Integer, nullable=False)  # soft FK in v1
    clip_ids: Mapped[str] = mapped_column(Text, nullable=False)  # comma-separated or JSON list
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    timeline_json: Mapped[dict | None] = mapped_column(JSONText, nullable=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class StorageQuota(Base):
    """Per-user / per-team storage quota row.

    Original v1.0-flask design was a singleton (id=1) tracking cluster-wide
    storage. Production schema actually has user_id + team_id columns —
    storage is tracked per (user, team) pair. Adopting prod's reality.
    """

    __tablename__ = "storage_quota"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)
    storage_used_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    storage_limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True, server_default="10737418240")  # 10 GiB — won't fit in INT
    video_ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="14")
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScoutingPlayer(Base):
    """Opponent player intel; per-user not tenant-scoped (coach shares across teams)."""

    __tablename__ = "scouting_players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    video_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # soft FK
    name: Mapped[str] = mapped_column(Text, nullable=False)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    dominant_hand: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    team_name: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    team_logo_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    photo_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())


class CompileCard(Base):
    """Compiled card (player card, opponent card, game plan card, ...).

    `config_json` (JSON-as-TEXT) holds card-type-specific configuration.
    """

    __tablename__ = "compile_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    card_type: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[dict] = mapped_column(JSONText, nullable=False, server_default="'{}'")
    video_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # soft FK
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())


__all__ = [
    "ScoutingVideo",
    "VideoClip",
    "VideoAnnotation",
    "ClipPlaylist",
    "PlaylistItem",
    "ClipShare",
    "StorageQuota",
    "ScoutingPlayer",
    "CompileCard",
]
