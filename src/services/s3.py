"""Async S3 service — port of `backend/scouting/s3_svc.py` to aioboto3.

Phase 6 batch 1. The endpoint flows that depend on this module:
  - GET  /api/scouting/upload-config        ← `is_s3_configured` + bucket info
  - POST /api/scouting/s3/presign-upload    ← `create_presigned_upload`
  - POST /api/scouting/s3/complete-multipart ← `complete_multipart`
  - GET  /api/scouting/video-proxy/{id}     ← `get_video_url` (presigned GET)
  - DELETE /api/scouting/videos/{id}        ← `delete_object`
  - POST /api/coach/avatar                  ← `put_object` (avatar bytes)

Async discipline:
  - aioboto3's `Session().client(...)` is an `async with` context manager.
    We wrap that as `async with s3_client() as s3` so every call site has
    explicit lifecycle (no leaked HTTP connections).
  - Sync-only client work (`generate_presigned_url`) is NOT awaitable in
    aioboto3 — the underlying botocore call is sync. aiobotocore patches
    the async ones; presign stays sync. No `await` for those.

Multi-tenancy in S3 keys (per master prompt §5 Phase 6):
  - Videos:  videos/{user_id}/{uuid12}/{safe_filename}.{ext}
  - Avatars: avatars/{uuid32}.webp           (NOT user-namespaced; matches v1)
  - Local fallback: local/{filename}         (dev mode without AWS creds)
The `user_id` field in the video key is enforced server-side from the
authenticated session. Coaches cannot upload to another coach's prefix.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from math import ceil
from typing import Any

import aioboto3
from botocore.config import Config

from src.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables — match v1 s3_svc.py:15-18
# ---------------------------------------------------------------------------

MULTIPART_THRESHOLD = 100 * 1024 * 1024  # 100 MB
PART_SIZE = 100 * 1024 * 1024            # 100 MB per part
PRESIGN_EXPIRY_UPLOAD = 3600             # 1 hour for upload
PRESIGN_EXPIRY_GET = 3600                # 1 hour for playback (regenerated each view)


# Allowed content-types for uploads — block executables, archives, etc.
_ALLOWED_UPLOAD_TYPES = frozenset({
    "video/mp4", "video/quicktime", "video/x-msvideo", "video/webm",
    "video/x-matroska",
    "image/jpeg", "image/png", "image/gif", "image/webp",
})


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------


def _aioboto3_session() -> aioboto3.Session:
    """Build a fresh Session per process. aioboto3 sessions are cheap to
    construct and credentials come from settings, not the env at call time."""
    return aioboto3.Session(
        region_name=settings.AWS_S3_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
    )


@asynccontextmanager
async def s3_client():
    """Async context manager yielding an aioboto3 S3 client.

    Use:
        async with s3_client() as s3:
            await s3.put_object(...)
    """
    session = _aioboto3_session()
    async with session.client(
        "s3",
        config=Config(signature_version="s3v4"),
    ) as client:
        yield client


def is_configured() -> bool:
    """True only when AWS credentials are present. Endpoints that need
    S3 should check this first and degrade to local mode otherwise."""
    return bool(
        (settings.AWS_ACCESS_KEY_ID or "").strip()
        and (settings.AWS_SECRET_ACCESS_KEY or "").strip()
    )


def get_upload_config() -> dict[str, Any]:
    """Frontend-safe S3 config (no secrets). Falls back to {provider:"local"}
    when AWS isn't configured — the SPA uses this to pick the upload path."""
    if not is_configured():
        return {"provider": "local"}
    return {
        "provider": "s3",
        "bucket": settings.AWS_S3_BUCKET,
        "region": settings.AWS_S3_REGION,
    }


# ---------------------------------------------------------------------------
# Filename sanitization — keep S3 keys ASCII-safe
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str) -> str:
    """Coerce a filename into ASCII-safe form. Hebrew / Unicode survive
    in display names but never in S3 keys (encoding mismatches between
    SDKs are a known pitfall). Mirror v1 _sanitize_filename."""
    parts = (name or "").rsplit(".", 1)
    ext = parts[1].lower() if len(parts) > 1 else "mp4"
    base = parts[0]
    base = re.sub(r"[^a-zA-Z0-9\-]", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    if not base:
        base = "video"
    return f"{base[:100]}.{ext}"


def _validate_content_type(content_type: str) -> str:
    """Strip codec parameters ("video/mp4;codecs=vp9") and validate.
    Raises ValueError on disallowed types — calling endpoint must
    surface as 400."""
    base = (content_type or "").split(";")[0].strip()
    if base not in _ALLOWED_UPLOAD_TYPES:
        raise ValueError(f"Content type '{content_type}' not allowed for upload")
    return base


# ---------------------------------------------------------------------------
# Presigned upload (single + multipart)
# ---------------------------------------------------------------------------


async def create_presigned_upload(
    *,
    file_name: str,
    file_size: int,
    content_type: str,
    user_id: int,
) -> dict[str, Any]:
    """Issue presigned URL(s) for a browser-direct S3 upload.

    Returns:
      file_size ≤ 100MB → {"mode": "single", "url": ..., "key": ...}
      file_size  > 100MB → {"mode": "multipart", "key", "upload_id",
                            "urls": [{"part_number", "url"}], "part_size"}

    Raises ValueError on disallowed content type. Bubble that up as
    HTTP 400 in the endpoint handler.
    """
    _validate_content_type(content_type)
    if int(file_size) <= 0:
        raise ValueError("file_size must be > 0")
    safe_name = _sanitize_filename(file_name)
    key = f"videos/{int(user_id)}/{uuid.uuid4().hex[:12]}/{safe_name}"

    async with s3_client() as s3:
        if file_size <= MULTIPART_THRESHOLD:
            url = await s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": settings.AWS_S3_BUCKET,
                    "Key": key,
                    "ContentType": content_type,
                },
                ExpiresIn=PRESIGN_EXPIRY_UPLOAD,
            )
            return {"mode": "single", "url": url, "key": key}

        # Multipart upload
        mpu = await s3.create_multipart_upload(
            Bucket=settings.AWS_S3_BUCKET,
            Key=key,
            ContentType=content_type,
        )
        upload_id = mpu["UploadId"]
        num_parts = ceil(file_size / PART_SIZE)
        urls: list[dict[str, Any]] = []
        for i in range(1, num_parts + 1):
            url = await s3.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": settings.AWS_S3_BUCKET,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": i,
                },
                ExpiresIn=PRESIGN_EXPIRY_UPLOAD,
            )
            urls.append({"part_number": i, "url": url})

    return {
        "mode": "multipart",
        "key": key,
        "upload_id": upload_id,
        "urls": urls,
        "part_size": PART_SIZE,
    }


async def complete_multipart(
    *,
    key: str,
    upload_id: str,
    parts: list[dict[str, Any]],
) -> None:
    """Finalize a multipart upload. `parts` is the list the browser
    collected from each PUT response — `[{part_number, etag}, ...]`."""
    if not parts:
        raise ValueError("parts list cannot be empty")
    sorted_parts = sorted(parts, key=lambda p: p["part_number"])
    async with s3_client() as s3:
        await s3.complete_multipart_upload(
            Bucket=settings.AWS_S3_BUCKET,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": p["part_number"], "ETag": p["etag"]}
                    for p in sorted_parts
                ]
            },
        )


# ---------------------------------------------------------------------------
# Read URL — CloudFront-prefixed or presigned GET
# ---------------------------------------------------------------------------


async def get_video_url(s3_key: str | None) -> str | None:
    """Return a playback URL for a stored asset. Returns None for empty key.

    Branches:
      - "local/<path>" → "/api/scouting/local-video/local/<path>" (dev fallback)
      - CloudFront configured → CloudFront URL (no presign)
      - Otherwise → presigned GET valid for 1 hour
    """
    if not s3_key:
        return None
    if s3_key.startswith("local/"):
        return f"/api/scouting/local-video/{s3_key}"
    if settings.CLOUDFRONT_DOMAIN:
        # CloudFront serves without a signed URL — origin access is via OAI/OAC
        # (configured outside the app). Mirrors v1 s3_svc.py:160-161.
        return f"https://{settings.CLOUDFRONT_DOMAIN}/{s3_key}"
    async with s3_client() as s3:
        return await s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.AWS_S3_BUCKET, "Key": s3_key},
            ExpiresIn=PRESIGN_EXPIRY_GET,
        )


# ---------------------------------------------------------------------------
# Delete + put (avatar)
# ---------------------------------------------------------------------------


async def delete_object(s3_key: str | None) -> bool:
    """Delete one S3 object. Returns True on success / no-op, False on
    real failure. Local-prefixed keys do nothing (file cleanup is the
    local-storage path's responsibility — Phase 7)."""
    if not s3_key:
        return True
    if s3_key.startswith("local/"):
        return True  # local file cleanup handled elsewhere
    try:
        async with s3_client() as s3:
            await s3.delete_object(
                Bucket=settings.AWS_S3_BUCKET, Key=s3_key,
            )
        return True
    except Exception as e:
        logger.error("[s3] delete failed for %s: %s", s3_key, e)
        return False


async def put_bytes(
    *,
    key: str,
    data: bytes,
    content_type: str,
) -> None:
    """Upload raw bytes to S3 — used by the avatar pipeline (PIL → WebP →
    S3 directly, no browser presign step). Raises on failure; the caller
    is responsible for surfacing a 500."""
    async with s3_client() as s3:
        await s3.put_object(
            Bucket=settings.AWS_S3_BUCKET,
            Key=key,
            Body=data,
            ContentType=content_type,
        )


__all__ = [
    "MULTIPART_THRESHOLD",
    "PART_SIZE",
    "PRESIGN_EXPIRY_GET",
    "PRESIGN_EXPIRY_UPLOAD",
    "complete_multipart",
    "create_presigned_upload",
    "delete_object",
    "get_upload_config",
    "get_video_url",
    "is_configured",
    "put_bytes",
    "s3_client",
]
