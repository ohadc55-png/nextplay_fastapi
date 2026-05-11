"""Scouting Room — async port of v1's scouting routes.

Phase 4 + 6 combined: CRUD for videos, clips, annotations, playlists,
scouting-players, compile-cards, plus quota readback, public clip-share
viewer, AND the S3 plumbing — upload-config, presigned uploads, multipart
completion, video proxy.

Endpoints (all under /api/scouting unless noted):

  Videos:      POST /videos | GET /videos | GET /videos/{id}
               PUT /videos/{id} | DELETE /videos/{id}
               POST /videos/external
  Clips:       POST /videos/{id}/clips
               PUT /clips/{id} | DELETE /clips/{id}
               POST /clips/batch-delete | POST /clips/batch-update
  Annotations: POST /videos/{id}/annotations | GET /videos/{id}/annotations
               PUT /annotations/{id} | DELETE /annotations/{id}
  Quota:       GET /quota
  Playlists:   POST /playlists | GET /playlists | GET /playlists/{id}
               DELETE /playlists/{id}
               POST /playlists/{id}/items | DELETE /playlists/{id}/items/{item_id}
               PUT /playlists/{id}/reorder
  Players:     POST /scouting-players | GET /scouting-players
               DELETE /scouting-players/{id}
  Cards:       POST /compile-cards | GET /compile-cards
               PUT /compile-cards/{id} | DELETE /compile-cards/{id}
  Share:       POST /clips/{id}/share | POST /clips/share-multi
               POST /share-timeline
               GET /share/{token}                (public, no auth)

All authed endpoints require Pro subscription (matches v1). The router
applies `require_pro` as a sub-dependency of `get_current_user` so admins
get a single 401/403 funnel.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import require_pro
from src.core.database import get_db
from src.models.scouting import (
    ClipPlaylist,
    ClipShare,
    CompileCard,
    PlaylistItem,
    ScoutingPlayer,
    ScoutingVideo,
    VideoAnnotation,
    VideoClip,
)
from src.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scouting", tags=["scouting"])


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _serialize_video(v: ScoutingVideo, *, clip_count: int | None = None) -> dict:
    return {
        "id": v.id, "user_id": v.user_id, "team_id": v.team_id,
        "title": v.title, "description": v.description or "",
        "video_type": v.video_type, "s3_key": v.s3_key, "s3_url": v.s3_url,
        "thumbnail_url": v.thumbnail_url, "original_name": v.original_name,
        "file_size": v.file_size, "duration_seconds": v.duration_seconds,
        "opponent": v.opponent, "game_date": v.game_date,
        "expires_at": v.expires_at.isoformat() if v.expires_at else None,
        "keep_forever": bool(v.keep_forever),
        "source_type": v.source_type, "external_url": v.external_url,
        "created_at": v.created_at, "updated_at": v.updated_at,
        "clip_count": clip_count,
    }


def _serialize_clip(c: VideoClip) -> dict:
    return {
        "id": c.id, "video_id": c.video_id, "title": c.title,
        "start_time": c.start_time, "end_time": c.end_time,
        "action_type": c.action_type, "rating": c.rating,
        "notes": c.notes, "created_at": c.created_at,
    }


def _decode_stroke(value: Any) -> Any:
    """v1 stores stroke_data as either a JSON-encoded string or a string-array;
    decode if it parses, otherwise pass through."""
    if isinstance(value, str) and value.startswith(("{", "[")):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _serialize_annotation(a: VideoAnnotation) -> dict:
    return {
        "id": a.id, "video_id": a.video_id, "clip_id": a.clip_id,
        "annotation_type": a.annotation_type, "timestamp": a.timestamp,
        "duration": a.duration, "stroke_data": _decode_stroke(a.stroke_data),
        "color": a.color, "stroke_width": a.stroke_width,
        "text_content": a.text_content, "created_at": a.created_at,
    }


async def _video_owned_by(db: AsyncSession, video_id: int, user_id: int) -> ScoutingVideo | None:
    return (await db.execute(
        select(ScoutingVideo).where(
            ScoutingVideo.id == video_id, ScoutingVideo.user_id == user_id
        )
    )).scalar_one_or_none()


async def _clip_owned_by(db: AsyncSession, clip_id: int, user_id: int) -> VideoClip | None:
    """Returns the clip iff the parent video belongs to user_id, else None."""
    row = (await db.execute(
        select(VideoClip)
        .join(ScoutingVideo, ScoutingVideo.id == VideoClip.video_id)
        .where(VideoClip.id == clip_id, ScoutingVideo.user_id == user_id)
    )).scalar_one_or_none()
    return row


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

class _VideoCreateBody(BaseModel):
    title: str | None = "Untitled"
    description: str | None = ""
    video_type: str | None = "game"
    s3_key: str | None = ""
    thumbnail_url: str | None = ""
    original_name: str | None = ""
    file_size: int | None = 0
    duration_seconds: float | None = 0
    opponent: str | None = ""
    game_date: str | None = ""
    keep_forever: bool | None = False


class _VideoExternalBody(BaseModel):
    url: str
    title: str | None = "Untitled"
    description: str | None = ""
    video_type: str | None = "game"
    opponent: str | None = ""
    game_date: str | None = ""


class _VideoUpdateBody(BaseModel):
    title: str | None = None
    description: str | None = None
    video_type: str | None = None
    opponent: str | None = None
    game_date: str | None = None
    keep_forever: bool | None = None


# ---------------------------------------------------------------------------
# S3 plumbing — upload config, presigned uploads, multipart, playback URL
# (Phase 6 batch 2/3)
# ---------------------------------------------------------------------------

class _PresignUploadBody(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    file_size: int = Field(gt=0, le=10 * 1024 * 1024 * 1024)  # cap 10 GB
    content_type: str = Field(min_length=1, max_length=100)


class _MultipartCompleteBody(BaseModel):
    key: str = Field(min_length=1, max_length=512)
    upload_id: str = Field(min_length=1, max_length=200)
    parts: list[dict[str, Any]] = Field(min_length=1, max_length=500)


@router.get("/upload-config")
async def upload_config(_user: User = Depends(require_pro)) -> dict:
    """Frontend-safe upload config. Returns provider="local" when AWS
    credentials are missing — the SPA falls back to /api/scouting/local/upload."""
    from src.services import s3 as s3_module

    return s3_module.get_upload_config()


@router.post("/s3/presign-upload")
async def presign_upload(
    body: _PresignUploadBody,
    user: User = Depends(require_pro),
) -> dict:
    """Issue presigned URL(s) so the browser can upload directly to S3.

    Returns single-PUT for files ≤100MB, multipart for larger. The S3
    key embeds `user_id` server-side — the client never controls it,
    so a coach cannot stash a file under another coach's prefix."""
    from src.services import s3 as s3_module

    if not s3_module.is_configured():
        raise HTTPException(status_code=503, detail="S3 is not configured on this server")
    try:
        return await s3_module.create_presigned_upload(
            file_name=body.file_name,
            file_size=body.file_size,
            content_type=body.content_type,
            user_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/s3/complete-multipart")
async def s3_complete_multipart(
    body: _MultipartCompleteBody,
    user: User = Depends(require_pro),
) -> dict:
    """Finalize a multipart upload — `parts` is the etag-list the
    browser collected after each PUT. Tenant guard: the `key` must be
    in this user's prefix (videos/<user_id>/...) otherwise we refuse —
    a coach cannot finalize an upload to another coach's prefix even
    if they somehow got the upload_id."""
    expected_prefix = f"videos/{user.id}/"
    if not body.key.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="Cross-tenant upload key")
    from src.services import s3 as s3_module

    try:
        await s3_module.complete_multipart(
            key=body.key, upload_id=body.upload_id, parts=body.parts,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


# ---------------------------------------------------------------------------
# SSRF protection + streaming proxy (ports v1 backend/scouting/routes.py:28-70
# and the Flask `video_proxy` / `public_share_video_proxy` handlers).
#
# Why a same-origin streaming proxy instead of a 302 to the presigned URL:
#   - <canvas> annotations (telestrator) only work when the <video> is loaded
#     same-origin; cross-origin tainting breaks getImageData / toDataURL.
#   - YouTube/Pixellot embeds aren't directly seekable from a <video> tag;
#     we let the coach feed the iframe URL but still proxy when possible.
#   - Range-request seeking has to be transparent — many CDNs strip auth
#     headers on redirect, so we re-issue the upstream request ourselves.
# ---------------------------------------------------------------------------

_ALLOWED_PROXY_DOMAINS = {
    ".s3.amazonaws.com",
    ".s3.eu-central-1.amazonaws.com",
    ".s3.us-east-1.amazonaws.com",
    ".s3.us-west-2.amazonaws.com",
    ".cloudfront.net",
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "vimeo.com",
    "player.vimeo.com",
}


def _is_safe_proxy_url(url: str | None) -> bool:
    """Return True only if `url` is safe to fetch from the server."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("https", "http"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # Block private / loopback / link-local / reserved IPs.
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            logger.warning("SSRF blocked: private IP %s", host)
            return False
    except ValueError:
        pass  # not an IP — check the domain whitelist below
    for allowed in _ALLOWED_PROXY_DOMAINS:
        if allowed.startswith("."):
            if host.endswith(allowed) or host == allowed[1:]:
                return True
        elif host == allowed:
            return True
    logger.warning("SSRF blocked: domain %s not in whitelist", host)
    return False


_PROXY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


async def _stream_video_proxy(request: Request, url: str) -> StreamingResponse:
    """Open an upstream GET (with Range header preserved) and stream the
    body back to the client. Falls back to a 502 on upstream errors.

    The httpx client + response are kept alive for the lifetime of the
    body iterator and closed in the `finally` block to avoid leaking
    connections from the pool.
    """
    if not _is_safe_proxy_url(url):
        raise HTTPException(status_code=403, detail="URL not allowed")

    upstream_headers: dict[str, str] = {"User-Agent": _PROXY_UA}
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
        follow_redirects=True,
    )
    try:
        req = client.build_request("GET", url, headers=upstream_headers)
        r = await client.send(req, stream=True)
    except httpx.RequestError as e:
        await client.aclose()
        logger.warning("[video-proxy] upstream error for %s: %s", url, e)
        raise HTTPException(status_code=502, detail="Upstream error") from None

    resp_headers: dict[str, str] = {
        "Content-Type": r.headers.get("content-type", "video/mp4"),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
    }
    for h in ("content-length", "content-range", "etag", "last-modified"):
        v = r.headers.get(h)
        if v:
            resp_headers[h.title()] = v

    async def _iter():
        try:
            async for chunk in r.aiter_bytes(chunk_size=131072):
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(
        _iter(),
        status_code=r.status_code,
        headers=resp_headers,
        media_type=resp_headers["Content-Type"],
    )


def _resolve_playback_url(v: ScoutingVideo, *, presign: Any) -> str | None:
    """Pick the upstream URL for a video, matching v1 semantics:
    external_url wins for source_type=external, otherwise presigned S3 URL.
    `presign` is the already-resolved S3 url (caller awaits it)."""
    if v.source_type == "external" and v.external_url:
        return v.external_url
    if v.source_type == "s3" and v.s3_key:
        return presign
    return None


@router.get("/video-proxy/{video_id}")
async def video_proxy(
    video_id: int,
    request: Request,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream a video through our server (same-origin) so the telestrator
    canvas can read its pixel data and the player can issue Range
    requests. Tenant-scoped: 404 unless the caller owns this video.

    Mirrors v1 backend/scouting/routes.py:172-209 byte-for-byte semantics
    (Range passthrough, User-Agent spoof, 131072-byte chunks)."""
    v = await _video_owned_by(db, video_id, user.id)
    if not v:
        raise HTTPException(status_code=404, detail="Not found")

    from src.services import s3 as s3_module

    presigned = await s3_module.get_video_url(v.s3_key or "") if v.s3_key else None
    url = _resolve_playback_url(v, presign=presigned)
    if not url:
        raise HTTPException(status_code=404, detail="Video has no playback source")
    return await _stream_video_proxy(request, url)


@router.get("/share/{token}/video")
async def public_share_video_proxy(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Public — no auth. Same proxy as `video_proxy` but authorized via
    the share token so anonymous clip-share viewers can play the video.
    Mirrors v1's `public_share_video_proxy`."""
    share = (await db.execute(
        select(ClipShare).where(ClipShare.share_token == token)
    )).scalar_one_or_none()
    if not share:
        raise HTTPException(status_code=404, detail="Not found")
    v = (await db.execute(
        select(ScoutingVideo).where(ScoutingVideo.id == share.video_id)
    )).scalar_one_or_none()
    if not v:
        raise HTTPException(status_code=404, detail="Not found")

    from src.services import s3 as s3_module

    presigned = await s3_module.get_video_url(v.s3_key or "") if v.s3_key else None
    url = _resolve_playback_url(v, presign=presigned)
    if not url:
        raise HTTPException(status_code=404, detail="Video has no playback source")
    return await _stream_video_proxy(request, url)


@router.post("/videos", status_code=201)
async def register_video(
    body: _VideoCreateBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Register a video after browser-side S3 upload completes.
    s3_key is captured but the actual upload itself happens via Phase 6
    presign endpoints."""
    ttl_days = 14  # default until quota row exists
    expires_at = (
        None if body.keep_forever
        else datetime.utcnow() + timedelta(days=ttl_days)
    )

    v = ScoutingVideo(
        user_id=user.id, team_id=user.active_team_id,
        title=body.title or "Untitled",
        description=body.description or "",
        video_type=body.video_type or "game",
        s3_key=body.s3_key or "",
        s3_url="",
        thumbnail_url=body.thumbnail_url or "",
        original_name=body.original_name or "",
        file_size=body.file_size or 0,
        duration_seconds=body.duration_seconds or 0,
        opponent=body.opponent or "",
        game_date=body.game_date or "",
        expires_at=expires_at,
        keep_forever=1 if body.keep_forever else 0,
        source_type="s3",
        created_at=_now(), updated_at=_now(),
    )
    db.add(v)
    await db.flush()
    return _serialize_video(v, clip_count=0)


@router.post("/videos/external", status_code=201)
async def register_external_video(
    body: _VideoExternalBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    url = body.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(
            status_code=400,
            detail="A valid URL starting with http:// or https:// is required",
        )
    v = ScoutingVideo(
        user_id=user.id, team_id=user.active_team_id,
        title=body.title or "Untitled",
        description=body.description or "",
        video_type=body.video_type or "game",
        s3_key="", s3_url="",
        original_name="", file_size=0,
        opponent=body.opponent or "", game_date=body.game_date or "",
        expires_at=None, keep_forever=1, source_type="external",
        external_url=url,
        created_at=_now(), updated_at=_now(),
    )
    db.add(v)
    await db.flush()
    return _serialize_video(v, clip_count=0)


@router.get("/videos")
async def list_videos(
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = list((await db.execute(
        select(ScoutingVideo)
        .where(ScoutingVideo.user_id == user.id)
        .order_by(ScoutingVideo.created_at.desc().nulls_last())
    )).scalars().all())
    out = []
    for v in rows:
        clip_count = int(await db.scalar(
            select(func.count()).select_from(VideoClip).where(VideoClip.video_id == v.id)
        ) or 0)
        out.append(_serialize_video(v, clip_count=clip_count))
    return out


@router.get("/videos/{video_id}")
async def get_video(
    video_id: int,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    v = await _video_owned_by(db, video_id, user.id)
    if not v:
        raise HTTPException(status_code=404, detail="Not found")
    clips = list((await db.execute(
        select(VideoClip)
        .where(VideoClip.video_id == video_id)
        .order_by(VideoClip.start_time)
    )).scalars().all())
    annotations = list((await db.execute(
        select(VideoAnnotation)
        .where(VideoAnnotation.video_id == video_id)
        .order_by(VideoAnnotation.timestamp)
    )).scalars().all())
    return {
        **_serialize_video(v, clip_count=len(clips)),
        "clips": [_serialize_clip(c) for c in clips],
        "annotations": [_serialize_annotation(a) for a in annotations],
    }


@router.put("/videos/{video_id}")
async def update_video(
    video_id: int,
    body: _VideoUpdateBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    v = await _video_owned_by(db, video_id, user.id)
    if not v:
        raise HTTPException(status_code=404, detail="Not found")

    data = body.model_dump(exclude_unset=True)
    for key in ("title", "description", "video_type", "opponent", "game_date"):
        if key in data:
            setattr(v, key, data[key])
    if "keep_forever" in data:
        flag = bool(data["keep_forever"])
        v.keep_forever = 1 if flag else 0
        if flag:
            v.expires_at = None
        else:
            v.expires_at = datetime.utcnow() + timedelta(days=14)
    v.updated_at = _now()
    await db.flush()
    return _serialize_video(v)


@router.delete("/videos/{video_id}")
async def delete_video(
    video_id: int,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    v = await _video_owned_by(db, video_id, user.id)
    if not v:
        raise HTTPException(status_code=404, detail="Not found")
    # Delete the S3 object first, then the DB row. Order matters: if S3
    # delete fails, the DB row stays so a sweep job can retry. The
    # opposite order would orphan the S3 object on the first failure.
    s3_key = v.s3_key or ""
    if s3_key and not s3_key.startswith("local/"):
        from src.services import s3 as s3_module

        ok = await s3_module.delete_object(s3_key)
        if not ok:
            logger.warning(
                "[scouting] S3 delete failed for video=%s key=%s — DB row kept",
                video_id, s3_key,
            )
            raise HTTPException(
                status_code=502,
                detail="Could not delete video file. Try again in a moment.",
            )
    await db.execute(delete(ScoutingVideo).where(ScoutingVideo.id == video_id))
    await db.flush()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

class _ClipCreateBody(BaseModel):
    title: str | None = "Clip"
    start_time: float
    end_time: float
    action_type: str | None = "other"
    rating: str | None = None
    notes: str | None = ""


class _ClipUpdateBody(BaseModel):
    title: str | None = None
    action_type: str | None = None
    rating: str | None = None
    notes: str | None = None


class _ClipBatchBody(BaseModel):
    clip_ids: list[int]
    rating: str | None = None


@router.post("/videos/{video_id}/clips", status_code=201)
async def create_clip(
    video_id: int,
    body: _ClipCreateBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not await _video_owned_by(db, video_id, user.id):
        raise HTTPException(status_code=404, detail="Not found")
    c = VideoClip(
        video_id=video_id, title=body.title or "Clip",
        start_time=body.start_time, end_time=body.end_time,
        action_type=body.action_type or "other",
        rating=body.rating, notes=body.notes or "",
        created_at=_now(),
    )
    db.add(c)
    await db.flush()
    return _serialize_clip(c)


@router.put("/clips/{clip_id}")
async def update_clip(
    clip_id: int,
    body: _ClipUpdateBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = await _clip_owned_by(db, clip_id, user.id)
    if not c:
        raise HTTPException(status_code=404, detail="Not found")
    data = body.model_dump(exclude_unset=True)
    for key in ("title", "action_type", "rating", "notes"):
        if key in data:
            setattr(c, key, data[key])
    await db.flush()
    return _serialize_clip(c)


@router.delete("/clips/{clip_id}")
async def delete_clip(
    clip_id: int,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not await _clip_owned_by(db, clip_id, user.id):
        raise HTTPException(status_code=404, detail="Not found")
    await db.execute(delete(VideoClip).where(VideoClip.id == clip_id))
    await db.flush()
    return {"ok": True}


@router.post("/clips/batch-delete")
async def batch_delete_clips(
    body: _ClipBatchBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not body.clip_ids:
        return {"ok": True}
    # Only delete clips whose parent video belongs to the caller
    owned_ids = list((await db.execute(
        select(VideoClip.id)
        .join(ScoutingVideo, ScoutingVideo.id == VideoClip.video_id)
        .where(VideoClip.id.in_(body.clip_ids), ScoutingVideo.user_id == user.id)
    )).scalars().all())
    if len(owned_ids) != len(body.clip_ids):
        raise HTTPException(status_code=404, detail="Not found")
    await db.execute(delete(VideoClip).where(VideoClip.id.in_(owned_ids)))
    await db.flush()
    return {"ok": True}


@router.post("/clips/batch-update")
async def batch_update_clips(
    body: _ClipBatchBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not body.clip_ids or body.rating is None:
        return {"ok": True}
    owned_ids = list((await db.execute(
        select(VideoClip.id)
        .join(ScoutingVideo, ScoutingVideo.id == VideoClip.video_id)
        .where(VideoClip.id.in_(body.clip_ids), ScoutingVideo.user_id == user.id)
    )).scalars().all())
    if len(owned_ids) != len(body.clip_ids):
        raise HTTPException(status_code=404, detail="Not found")
    from sqlalchemy import update
    await db.execute(
        update(VideoClip).where(VideoClip.id.in_(owned_ids)).values(rating=body.rating)
    )
    await db.flush()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

class _AnnotationCreateBody(BaseModel):
    annotation_type: str  # drawing | text | arrow | highlight
    timestamp: float
    clip_id: int | None = None
    duration: float | None = 3.0
    stroke_data: Any | None = None
    color: str | None = "#FF0000"
    stroke_width: int | None = 3
    text_content: str | None = None


class _AnnotationUpdateBody(BaseModel):
    annotation_type: str | None = None
    timestamp: float | None = None
    duration: float | None = None
    stroke_data: Any | None = None
    color: str | None = None
    stroke_width: int | None = None
    text_content: str | None = None


@router.post("/videos/{video_id}/annotations", status_code=201)
async def create_annotation(
    video_id: int,
    body: _AnnotationCreateBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not await _video_owned_by(db, video_id, user.id):
        raise HTTPException(status_code=404, detail="Not found")
    stroke = body.stroke_data
    if isinstance(stroke, (dict, list)):
        stroke = json.dumps(stroke)
    ann = VideoAnnotation(
        video_id=video_id, clip_id=body.clip_id,
        annotation_type=body.annotation_type,
        timestamp=body.timestamp,
        duration=body.duration if body.duration is not None else 3.0,
        stroke_data=stroke,
        color=body.color or "#FF0000",
        stroke_width=body.stroke_width if body.stroke_width is not None else 3,
        text_content=body.text_content,
        created_at=_now(),
    )
    db.add(ann)
    await db.flush()
    return _serialize_annotation(ann)


@router.get("/videos/{video_id}/annotations")
async def get_annotations(
    video_id: int,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    if not await _video_owned_by(db, video_id, user.id):
        raise HTTPException(status_code=404, detail="Not found")
    rows = list((await db.execute(
        select(VideoAnnotation)
        .where(VideoAnnotation.video_id == video_id)
        .order_by(VideoAnnotation.timestamp)
    )).scalars().all())
    return [_serialize_annotation(a) for a in rows]


@router.put("/annotations/{ann_id}")
async def update_annotation(
    ann_id: int,
    body: _AnnotationUpdateBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ann = (await db.execute(
        select(VideoAnnotation)
        .join(ScoutingVideo, ScoutingVideo.id == VideoAnnotation.video_id)
        .where(VideoAnnotation.id == ann_id, ScoutingVideo.user_id == user.id)
    )).scalar_one_or_none()
    if not ann:
        raise HTTPException(status_code=404, detail="Not found")

    data = body.model_dump(exclude_unset=True)
    for key in ("annotation_type", "timestamp", "duration", "color",
                "stroke_width", "text_content"):
        if key in data:
            setattr(ann, key, data[key])
    if "stroke_data" in data:
        sd = data["stroke_data"]
        ann.stroke_data = json.dumps(sd) if isinstance(sd, (dict, list)) else sd
    await db.flush()
    return _serialize_annotation(ann)


@router.delete("/annotations/{ann_id}")
async def delete_annotation(
    ann_id: int,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ann = (await db.execute(
        select(VideoAnnotation)
        .join(ScoutingVideo, ScoutingVideo.id == VideoAnnotation.video_id)
        .where(VideoAnnotation.id == ann_id, ScoutingVideo.user_id == user.id)
    )).scalar_one_or_none()
    if not ann:
        raise HTTPException(status_code=404, detail="Not found")
    await db.execute(delete(VideoAnnotation).where(VideoAnnotation.id == ann_id))
    await db.flush()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Quota — calculated live from scouting_videos.file_size
# ---------------------------------------------------------------------------

@router.get("/quota")
async def get_quota(
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Storage quota for the current (user, team). The v1 club-pooled
    branch is not wired here — Phase 6/7 picks up club aggregations."""
    used = int(await db.scalar(
        select(func.coalesce(func.sum(ScoutingVideo.file_size), 0))
        .where(ScoutingVideo.user_id == user.id)
    ) or 0)

    extra_gb = 0
    total_gb = 10
    if user.subscription_plan == "trial":
        total_gb = min(total_gb, 1)

    return {
        "storage_used_bytes": used,
        "storage_limit_gb": total_gb + extra_gb,
        "storage_limit_bytes": (total_gb + extra_gb) * 1073741824,
        "extra_storage_gb": extra_gb,
        "video_ttl_days": 14,
    }


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

class _PlaylistCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = ""


class _PlaylistItemAddBody(BaseModel):
    clip_id: int
    sort_order: int | None = 0


class _PlaylistReorderBody(BaseModel):
    item_ids: list[int]


def _serialize_playlist(p: ClipPlaylist, *, item_count: int = 0) -> dict:
    return {
        "id": p.id, "user_id": p.user_id, "team_id": p.team_id,
        "name": p.name, "description": p.description or "",
        "created_at": p.created_at, "updated_at": p.updated_at,
        "item_count": item_count,
    }


async def _playlist_owned_by(
    db: AsyncSession, playlist_id: int, user_id: int
) -> ClipPlaylist | None:
    return (await db.execute(
        select(ClipPlaylist).where(
            ClipPlaylist.id == playlist_id, ClipPlaylist.user_id == user_id
        )
    )).scalar_one_or_none()


@router.post("/playlists", status_code=201)
async def create_playlist(
    body: _PlaylistCreateBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    p = ClipPlaylist(
        user_id=user.id, team_id=user.active_team_id,
        name=body.name, description=body.description or "",
        created_at=_now(), updated_at=_now(),
    )
    db.add(p)
    await db.flush()
    return _serialize_playlist(p)


@router.get("/playlists")
async def list_playlists(
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = list((await db.execute(
        select(ClipPlaylist)
        .where(ClipPlaylist.user_id == user.id)
        .order_by(ClipPlaylist.created_at.desc().nulls_last())
    )).scalars().all())
    out = []
    for p in rows:
        item_count = int(await db.scalar(
            select(func.count()).select_from(PlaylistItem)
            .where(PlaylistItem.playlist_id == p.id)
        ) or 0)
        out.append(_serialize_playlist(p, item_count=item_count))
    return out


@router.get("/playlists/{playlist_id}")
async def get_playlist(
    playlist_id: int,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    p = await _playlist_owned_by(db, playlist_id, user.id)
    if not p:
        raise HTTPException(status_code=404, detail="Not found")
    items = (await db.execute(
        select(
            PlaylistItem.id, PlaylistItem.clip_id, PlaylistItem.sort_order,
            PlaylistItem.note,
            VideoClip.title.label("clip_title"),
            VideoClip.start_time, VideoClip.end_time,
            VideoClip.action_type, VideoClip.rating,
            VideoClip.video_id,
        )
        .join(VideoClip, VideoClip.id == PlaylistItem.clip_id)
        .where(PlaylistItem.playlist_id == playlist_id)
        .order_by(PlaylistItem.sort_order)
    )).mappings().all()
    return {
        **_serialize_playlist(p, item_count=len(items)),
        "items": [dict(r) for r in items],
    }


@router.delete("/playlists/{playlist_id}")
async def delete_playlist(
    playlist_id: int,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not await _playlist_owned_by(db, playlist_id, user.id):
        raise HTTPException(status_code=404, detail="Not found")
    await db.execute(delete(ClipPlaylist).where(ClipPlaylist.id == playlist_id))
    await db.flush()
    return {"ok": True}


@router.post("/playlists/{playlist_id}/items", status_code=201)
async def add_playlist_item(
    playlist_id: int,
    body: _PlaylistItemAddBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not await _playlist_owned_by(db, playlist_id, user.id):
        raise HTTPException(status_code=404, detail="Not found")
    if not await _clip_owned_by(db, body.clip_id, user.id):
        raise HTTPException(status_code=404, detail="Not found")

    # Idempotent on UNIQUE(playlist_id, clip_id) — re-adds are silent.
    existing = (await db.execute(
        select(PlaylistItem.id).where(
            PlaylistItem.playlist_id == playlist_id,
            PlaylistItem.clip_id == body.clip_id,
        )
    )).scalar_one_or_none()
    if not existing:
        db.add(PlaylistItem(
            playlist_id=playlist_id, clip_id=body.clip_id,
            sort_order=body.sort_order or 0,
        ))
        await db.flush()
    return {"ok": True}


@router.delete("/playlists/{playlist_id}/items/{item_id}")
async def remove_playlist_item(
    playlist_id: int,
    item_id: int,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not await _playlist_owned_by(db, playlist_id, user.id):
        raise HTTPException(status_code=404, detail="Not found")
    await db.execute(
        delete(PlaylistItem).where(
            PlaylistItem.id == item_id, PlaylistItem.playlist_id == playlist_id
        )
    )
    await db.flush()
    return {"ok": True}


@router.put("/playlists/{playlist_id}/reorder")
async def reorder_playlist(
    playlist_id: int,
    body: _PlaylistReorderBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not await _playlist_owned_by(db, playlist_id, user.id):
        raise HTTPException(status_code=404, detail="Not found")
    from sqlalchemy import update as _update
    for i, item_id in enumerate(body.item_ids):
        await db.execute(
            _update(PlaylistItem)
            .where(PlaylistItem.id == item_id, PlaylistItem.playlist_id == playlist_id)
            .values(sort_order=i)
        )
    await db.flush()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Scouting players (per-user opponent profiles)
# ---------------------------------------------------------------------------

class _ScoutingPlayerCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    video_id: int | None = None
    number: int | None = None
    position: str | None = ""
    dominant_hand: str | None = ""
    team_name: str | None = ""
    notes: str | None = ""


def _serialize_scouting_player(p: ScoutingPlayer) -> dict:
    return {
        "id": p.id, "user_id": p.user_id, "video_id": p.video_id,
        "name": p.name, "number": p.number, "position": p.position or "",
        "dominant_hand": p.dominant_hand or "",
        "team_name": p.team_name or "",
        "team_logo_s3_key": p.team_logo_s3_key or "",
        "photo_s3_key": p.photo_s3_key or "",
        "notes": p.notes or "",
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.post("/scouting-players", status_code=201)
async def create_scouting_player(
    body: _ScoutingPlayerCreateBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    p = ScoutingPlayer(
        user_id=user.id, video_id=body.video_id, name=body.name,
        number=body.number, position=body.position or "",
        dominant_hand=body.dominant_hand or "",
        team_name=body.team_name or "", notes=body.notes or "",
    )
    db.add(p)
    await db.flush()
    return _serialize_scouting_player(p)


@router.get("/scouting-players")
async def list_scouting_players(
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
    video_id: int | None = None,
) -> list[dict]:
    stmt = select(ScoutingPlayer).where(ScoutingPlayer.user_id == user.id)
    if video_id is not None:
        stmt = stmt.where(ScoutingPlayer.video_id == video_id)
    stmt = stmt.order_by(ScoutingPlayer.created_at.desc().nulls_last())
    rows = list((await db.execute(stmt)).scalars().all())
    return [_serialize_scouting_player(p) for p in rows]


@router.delete("/scouting-players/{player_id}")
async def delete_scouting_player(
    player_id: int,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await db.execute(
        delete(ScoutingPlayer).where(
            ScoutingPlayer.id == player_id,
            ScoutingPlayer.user_id == user.id,
        )
    )
    await db.flush()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Compile cards
# ---------------------------------------------------------------------------

class _CompileCardCreateBody(BaseModel):
    card_type: str
    config: dict | None = None
    video_id: int | None = None


class _CompileCardUpdateBody(BaseModel):
    config: dict | None = None
    card_type: str | None = None
    video_id: int | None = None


def _serialize_card(c: CompileCard) -> dict:
    return {
        "id": c.id, "user_id": c.user_id, "card_type": c.card_type,
        "config": c.config_json or {}, "video_id": c.video_id,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


@router.post("/compile-cards", status_code=201)
async def create_compile_card(
    body: _CompileCardCreateBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = CompileCard(
        user_id=user.id, card_type=body.card_type,
        config_json=body.config or {}, video_id=body.video_id,
    )
    db.add(c)
    await db.flush()
    return _serialize_card(c)


@router.get("/compile-cards")
async def list_compile_cards(
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
    video_id: int | None = None,
) -> list[dict]:
    stmt = select(CompileCard).where(CompileCard.user_id == user.id)
    if video_id is not None:
        stmt = stmt.where(CompileCard.video_id == video_id)
    stmt = stmt.order_by(CompileCard.created_at.desc().nulls_last())
    rows = list((await db.execute(stmt)).scalars().all())
    return [_serialize_card(c) for c in rows]


@router.put("/compile-cards/{card_id}")
async def update_compile_card(
    card_id: int,
    body: _CompileCardUpdateBody,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = (await db.execute(
        select(CompileCard).where(
            CompileCard.id == card_id, CompileCard.user_id == user.id
        )
    )).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Not found")
    data = body.model_dump(exclude_unset=True)
    if "config" in data:
        c.config_json = data["config"] or {}
    if "card_type" in data:
        c.card_type = data["card_type"]
    if "video_id" in data:
        c.video_id = data["video_id"]
    c.updated_at = datetime.utcnow()
    await db.flush()
    return _serialize_card(c)


@router.delete("/compile-cards/{card_id}")
async def delete_compile_card(
    card_id: int,
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await db.execute(
        delete(CompileCard).where(
            CompileCard.id == card_id, CompileCard.user_id == user.id
        )
    )
    await db.flush()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Public clip share
# ---------------------------------------------------------------------------

@router.post("/clips/{clip_id}/share", status_code=201)
async def share_clip(
    clip_id: int,
    request: Request,
    body: dict = Body(default_factory=dict),
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Snapshot a single clip under a public token. Idempotent on
    (video_id, clip_ids) — re-sharing the same set returns the existing token."""
    video_id = body.get("video_id")
    if not video_id:
        raise HTTPException(status_code=400, detail="video_id is required")
    if not await _video_owned_by(db, int(video_id), user.id):
        raise HTTPException(status_code=404, detail="Not found")
    if not await _clip_owned_by(db, clip_id, user.id):
        raise HTTPException(status_code=404, detail="Not found")

    token = await _create_or_reuse_share(db, video_id=int(video_id), clip_ids=[clip_id], user_id=user.id)
    base_url = str(request.base_url).rstrip("/")
    return {"token": token, "url": f"{base_url}/share/{token}"}


@router.post("/clips/share-multi", status_code=201)
async def share_clips_multi(
    request: Request,
    body: dict = Body(...),
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    video_id = body.get("video_id")
    clip_ids = body.get("clip_ids") or []
    if not video_id or not clip_ids:
        raise HTTPException(status_code=400, detail="video_id and clip_ids are required")
    if not await _video_owned_by(db, int(video_id), user.id):
        raise HTTPException(status_code=404, detail="Not found")
    for cid in clip_ids:
        if not await _clip_owned_by(db, int(cid), user.id):
            raise HTTPException(status_code=404, detail="Not found")

    token = await _create_or_reuse_share(
        db, video_id=int(video_id), clip_ids=[int(c) for c in clip_ids], user_id=user.id,
    )
    base_url = str(request.base_url).rstrip("/")
    return {"token": token, "url": f"{base_url}/share/{token}"}


@router.post("/share-timeline", status_code=201)
async def share_timeline(
    request: Request,
    body: dict = Body(...),
    user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> dict:
    video_id = body.get("video_id")
    timeline = body.get("timeline") or []
    if not video_id or not timeline:
        raise HTTPException(status_code=400, detail="video_id and timeline are required")
    clip_ids = [
        int(item["clip_id"])
        for item in timeline
        if isinstance(item, dict) and item.get("type") == "clip" and item.get("clip_id")
    ]
    if not clip_ids:
        raise HTTPException(status_code=400, detail="Timeline must contain at least one clip")
    if not await _video_owned_by(db, int(video_id), user.id):
        raise HTTPException(status_code=404, detail="Not found")
    for cid in clip_ids:
        if not await _clip_owned_by(db, cid, user.id):
            raise HTTPException(status_code=404, detail="Not found")

    token = uuid.uuid4().hex
    db.add(ClipShare(
        share_token=token,
        video_id=int(video_id),
        clip_ids=",".join(str(c) for c in sorted(clip_ids)),
        created_by=user.id,
        timeline_json=timeline,
        created_at=_now(),
    ))
    await db.flush()
    base_url = str(request.base_url).rstrip("/")
    return {"token": token, "url": f"{base_url}/share/{token}"}


async def _create_or_reuse_share(
    db: AsyncSession, *, video_id: int, clip_ids: list[int], user_id: int
) -> str:
    """Mirrors v1 create_share — dedupe on (video_id, sorted clip_ids)."""
    clip_ids_str = ",".join(str(c) for c in sorted(clip_ids))
    existing = (await db.execute(
        select(ClipShare.share_token).where(
            ClipShare.video_id == video_id,
            ClipShare.clip_ids == clip_ids_str,
        )
    )).scalar_one_or_none()
    if existing:
        return existing
    token = uuid.uuid4().hex
    db.add(ClipShare(
        share_token=token, video_id=video_id, clip_ids=clip_ids_str,
        created_by=user_id, created_at=_now(),
    ))
    await db.flush()
    return token


@router.get("/share/{token}")
async def public_share(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Public — no auth. Returns the snapshot's video + clips + annotations
    so the frontend's clip_share.html can render."""
    share = (await db.execute(
        select(ClipShare).where(ClipShare.share_token == token)
    )).scalar_one_or_none()
    if not share:
        raise HTTPException(status_code=404, detail="Clip not found")

    video = (await db.execute(
        select(ScoutingVideo).where(ScoutingVideo.id == share.video_id)
    )).scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Clip not found")

    clip_ids = [int(x) for x in (share.clip_ids or "").split(",") if x.strip()]
    clips = list((await db.execute(
        select(VideoClip).where(VideoClip.id.in_(clip_ids))
        .order_by(VideoClip.start_time)
    )).scalars().all()) if clip_ids else []
    annotations = list((await db.execute(
        select(VideoAnnotation).where(VideoAnnotation.video_id == share.video_id)
        .order_by(VideoAnnotation.timestamp)
    )).scalars().all())

    return {
        "video": _serialize_video(video),
        "clip": _serialize_clip(clips[0]) if clips else None,
        "clips": [_serialize_clip(c) for c in clips],
        "annotations": [_serialize_annotation(a) for a in annotations],
        "timeline": share.timeline_json,
        "share": {
            "token": share.share_token, "video_id": share.video_id,
            "created_at": share.created_at,
        },
    }
