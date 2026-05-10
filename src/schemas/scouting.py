"""Scouting + video room schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel

# ---------------------------------------------------------------------------
# Scouting videos
# ---------------------------------------------------------------------------

class ScoutingVideoCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = ""
    video_type: str | None = "game"
    s3_key: str | None = None
    s3_url: str | None = None
    thumbnail_url: str | None = None
    original_name: str | None = None
    file_size: int | None = 0
    duration_seconds: float | None = 0
    opponent: str | None = ""
    game_date: str | None = ""
    keep_forever: int | None = 0
    source_type: str | None = "s3"
    external_url: str | None = None


class ScoutingVideoUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    video_type: str | None = None
    opponent: str | None = None
    game_date: str | None = None
    keep_forever: int | None = None


class ScoutingVideoResponse(ScoutingVideoCreate, ORMModel):
    id: int
    user_id: int | None = None
    team_id: int | None = None
    expires_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


# ---------------------------------------------------------------------------
# Direct-to-S3 upload signing
# ---------------------------------------------------------------------------

class S3PresignUploadRequest(BaseModel):
    filename: str
    file_type: str
    file_size: int


class S3PresignUploadResponse(BaseModel):
    upload_url: str
    fields: dict
    s3_key: str


class S3CompleteMultipartRequest(BaseModel):
    s3_key: str
    upload_id: str
    parts: list[dict]


# ---------------------------------------------------------------------------
# Clips + annotations
# ---------------------------------------------------------------------------

class VideoClipCreate(BaseModel):
    video_id: int
    title: str
    start_time: float
    end_time: float
    action_type: str | None = "other"
    rating: str | None = None
    notes: str | None = ""


class VideoClipResponse(VideoClipCreate, ORMModel):
    id: int
    created_at: str | None = None


class VideoAnnotationCreate(BaseModel):
    video_id: int
    clip_id: int | None = None
    annotation_type: str  # drawing | text | arrow | highlight
    timestamp: float
    duration: float | None = 3.0
    stroke_data: str | None = None
    color: str | None = "#FF0000"
    stroke_width: int | None = 3
    text_content: str | None = None


class VideoAnnotationResponse(VideoAnnotationCreate, ORMModel):
    id: int
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Playlists + items
# ---------------------------------------------------------------------------

class ClipPlaylistCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = ""


class ClipPlaylistResponse(ORMModel):
    id: int
    user_id: int | None = None
    team_id: int | None = None
    name: str
    description: str | None = None
    created_at: str | None = None


class PlaylistItemCreate(BaseModel):
    playlist_id: int
    clip_id: int
    sort_order: int | None = 0
    note: str | None = ""


# ---------------------------------------------------------------------------
# Public clip share
# ---------------------------------------------------------------------------

class ClipShareCreate(BaseModel):
    video_id: int
    clip_ids: list[int]
    timeline_json: dict | None = None


class ClipShareResponse(ORMModel):
    id: int
    share_token: str
    video_id: int
    clip_ids: str  # serialized list / JSON
    timeline_json: dict | None = None
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Scouting players + compile cards
# ---------------------------------------------------------------------------

class ScoutingPlayerCreate(BaseModel):
    name: str
    number: int | None = None
    position: str | None = ""
    dominant_hand: str | None = ""
    team_name: str | None = ""
    team_logo_s3_key: str | None = ""
    photo_s3_key: str | None = ""
    notes: str | None = ""
    video_id: int | None = None


class ScoutingPlayerResponse(ScoutingPlayerCreate, ORMModel):
    id: int
    user_id: int


class CompileCardCreate(BaseModel):
    card_type: str
    config_json: dict = Field(default_factory=dict)
    video_id: int | None = None


class CompileCardResponse(CompileCardCreate, ORMModel):
    id: int
    user_id: int
    created_at: str | None = None
    updated_at: str | None = None


__all__ = [
    "ClipPlaylistCreate",
    "ClipPlaylistResponse",
    "ClipShareCreate",
    "ClipShareResponse",
    "CompileCardCreate",
    "CompileCardResponse",
    "PlaylistItemCreate",
    "S3CompleteMultipartRequest",
    "S3PresignUploadRequest",
    "S3PresignUploadResponse",
    "ScoutingPlayerCreate",
    "ScoutingPlayerResponse",
    "ScoutingVideoCreate",
    "ScoutingVideoResponse",
    "ScoutingVideoUpdate",
    "VideoAnnotationCreate",
    "VideoAnnotationResponse",
    "VideoClipCreate",
    "VideoClipResponse",
]
