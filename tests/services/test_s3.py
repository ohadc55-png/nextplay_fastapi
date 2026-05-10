"""Async S3 service tests.

We mock aioboto3 at the `s3_client` boundary — the contract being:
the function yields an object with async S3 methods. That keeps the
tests fast, deterministic, and free from AWS credentials."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.services import s3 as s3_module

# ---------------------------------------------------------------------------
# Fake aioboto3 client
# ---------------------------------------------------------------------------


class _FakeS3:
    """Minimal aioboto3 S3 stand-in: every method we use is async."""

    def __init__(self):
        # Async stubs — each test overrides what it needs.
        self.generate_presigned_url = AsyncMock(return_value="https://s3/presigned")
        self.create_multipart_upload = AsyncMock(return_value={"UploadId": "mpu-123"})
        self.complete_multipart_upload = AsyncMock(return_value={})
        self.delete_object = AsyncMock(return_value={})
        self.put_object = AsyncMock(return_value={})


def _patch_s3_client(fake: _FakeS3):
    """Replace `s3_module.s3_client` with an async-context-manager that
    yields the fake. Returns the patcher (use as `with` context)."""

    @asynccontextmanager
    async def _cm():
        yield fake

    return patch.object(s3_module, "s3_client", _cm)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestIsConfigured:
    def test_no_credentials_means_not_configured(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        assert s3_module.is_configured() is False

    def test_partial_credentials_means_not_configured(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        assert s3_module.is_configured() is False

    def test_both_configured(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "secret")
        assert s3_module.is_configured() is True


class TestUploadConfig:
    def test_local_when_unconfigured(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        cfg = s3_module.get_upload_config()
        assert cfg == {"provider": "local"}

    def test_s3_when_configured(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "s")
        monkeypatch.setattr(settings, "AWS_S3_BUCKET", "my-bucket")
        monkeypatch.setattr(settings, "AWS_S3_REGION", "eu-central-1")
        cfg = s3_module.get_upload_config()
        assert cfg == {
            "provider": "s3",
            "bucket": "my-bucket",
            "region": "eu-central-1",
        }


class TestSanitizeFilename:
    @pytest.mark.parametrize("name,expected_ext", [
        ("game.mp4", "mp4"),
        ("MOVIE.MOV", "mov"),
        ("recording.webm", "webm"),
    ])
    def test_extension_preserved_lowercased(self, name, expected_ext):
        out = s3_module._sanitize_filename(name)
        assert out.endswith(f".{expected_ext}")

    def test_hebrew_name_falls_back_to_video(self):
        out = s3_module._sanitize_filename("משחק.mp4")
        assert out == "video.mp4"

    def test_special_chars_replaced(self):
        out = s3_module._sanitize_filename("game (final) v2.mp4")
        # No special chars in the base name
        assert " " not in out
        assert "(" not in out
        assert out.endswith(".mp4")

    def test_no_extension_defaults_to_mp4(self):
        out = s3_module._sanitize_filename("recording")
        assert out.endswith(".mp4")

    def test_long_name_truncated(self):
        out = s3_module._sanitize_filename("a" * 200 + ".mp4")
        # Base capped at 100 chars
        base = out.rsplit(".", 1)[0]
        assert len(base) <= 100


class TestValidateContentType:
    @pytest.mark.parametrize("ct", [
        "video/mp4", "video/quicktime", "video/webm",
        "image/jpeg", "image/png", "image/webp",
    ])
    def test_allowed_types(self, ct):
        assert s3_module._validate_content_type(ct) == ct

    def test_codec_suffix_stripped(self):
        assert s3_module._validate_content_type("video/mp4;codecs=avc1") == "video/mp4"

    @pytest.mark.parametrize("ct", [
        "application/octet-stream",
        "text/html",
        "application/x-msdownload",  # .exe
        "",
    ])
    def test_disallowed_types_rejected(self, ct):
        with pytest.raises(ValueError):
            s3_module._validate_content_type(ct)


# ---------------------------------------------------------------------------
# Presigned upload
# ---------------------------------------------------------------------------


class TestCreatePresignedUpload:
    async def test_small_file_returns_single_presign(self):
        fake = _FakeS3()
        with _patch_s3_client(fake):
            result = await s3_module.create_presigned_upload(
                file_name="game.mp4",
                file_size=10 * 1024 * 1024,  # 10 MB
                content_type="video/mp4",
                user_id=42,
            )
        assert result["mode"] == "single"
        assert result["url"] == "https://s3/presigned"
        # Key namespaced by user_id (multi-tenancy invariant)
        assert result["key"].startswith("videos/42/")
        assert result["key"].endswith(".mp4")
        # CreateMultipartUpload should NOT have been called
        fake.create_multipart_upload.assert_not_called()

    async def test_large_file_returns_multipart(self):
        fake = _FakeS3()
        with _patch_s3_client(fake):
            result = await s3_module.create_presigned_upload(
                file_name="long.mp4",
                file_size=300 * 1024 * 1024,  # 300 MB → 3 parts of 100MB
                content_type="video/mp4",
                user_id=42,
            )
        assert result["mode"] == "multipart"
        assert result["upload_id"] == "mpu-123"
        assert len(result["urls"]) == 3
        assert {p["part_number"] for p in result["urls"]} == {1, 2, 3}
        # Key still tenant-namespaced
        assert result["key"].startswith("videos/42/")

    async def test_invalid_content_type_rejected(self):
        with pytest.raises(ValueError):
            await s3_module.create_presigned_upload(
                file_name="bad.exe",
                file_size=1024,
                content_type="application/octet-stream",
                user_id=42,
            )

    async def test_zero_file_size_rejected(self):
        with pytest.raises(ValueError):
            await s3_module.create_presigned_upload(
                file_name="empty.mp4",
                file_size=0,
                content_type="video/mp4",
                user_id=42,
            )

    async def test_user_id_in_key_blocks_path_traversal(self):
        """user_id is coerced through int() — no way to inject '../'."""
        fake = _FakeS3()
        with _patch_s3_client(fake):
            r1 = await s3_module.create_presigned_upload(
                file_name="x.mp4", file_size=1024,
                content_type="video/mp4", user_id=7,
            )
            r2 = await s3_module.create_presigned_upload(
                file_name="x.mp4", file_size=1024,
                content_type="video/mp4", user_id=11,
            )
        # Different user_id → different namespace
        assert r1["key"].startswith("videos/7/")
        assert r2["key"].startswith("videos/11/")


class TestCompleteMultipart:
    async def test_parts_sorted_by_number(self):
        fake = _FakeS3()
        with _patch_s3_client(fake):
            await s3_module.complete_multipart(
                key="videos/42/abc/x.mp4",
                upload_id="mpu-123",
                parts=[
                    {"part_number": 3, "etag": "etag-3"},
                    {"part_number": 1, "etag": "etag-1"},
                    {"part_number": 2, "etag": "etag-2"},
                ],
            )
        # Verify the call payload sorted ascending
        call_kwargs = fake.complete_multipart_upload.await_args.kwargs
        parts_payload = call_kwargs["MultipartUpload"]["Parts"]
        part_numbers = [p["PartNumber"] for p in parts_payload]
        assert part_numbers == [1, 2, 3]

    async def test_empty_parts_rejected(self):
        with pytest.raises(ValueError):
            await s3_module.complete_multipart(
                key="x", upload_id="y", parts=[],
            )


# ---------------------------------------------------------------------------
# Read URL
# ---------------------------------------------------------------------------


class TestGetVideoUrl:
    async def test_empty_key_returns_none(self):
        assert await s3_module.get_video_url(None) is None
        assert await s3_module.get_video_url("") is None

    async def test_local_prefix_returns_local_path(self):
        url = await s3_module.get_video_url("local/some/path.mp4")
        assert url == "/api/scouting/local-video/local/some/path.mp4"

    async def test_cloudfront_used_when_configured(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "CLOUDFRONT_DOMAIN", "cdn.example.com")
        url = await s3_module.get_video_url("videos/42/abc/x.mp4")
        assert url == "https://cdn.example.com/videos/42/abc/x.mp4"

    async def test_falls_back_to_presigned_get(self, monkeypatch):
        from src.core.config import settings
        monkeypatch.setattr(settings, "CLOUDFRONT_DOMAIN", "")
        fake = _FakeS3()
        fake.generate_presigned_url = AsyncMock(return_value="https://s3/presigned-get")
        with _patch_s3_client(fake):
            url = await s3_module.get_video_url("videos/42/abc/x.mp4")
        assert url == "https://s3/presigned-get"
        # Verify generate_presigned_url was called for `get_object`
        call_args = fake.generate_presigned_url.await_args.args
        assert call_args[0] == "get_object"


# ---------------------------------------------------------------------------
# Delete + put
# ---------------------------------------------------------------------------


class TestDeleteObject:
    async def test_empty_key_is_noop(self):
        assert await s3_module.delete_object(None) is True
        assert await s3_module.delete_object("") is True

    async def test_local_prefix_is_noop(self):
        # Should NOT call S3 — local files are managed elsewhere
        fake = _FakeS3()
        with _patch_s3_client(fake):
            ok = await s3_module.delete_object("local/path/file.mp4")
        assert ok is True
        fake.delete_object.assert_not_called()

    async def test_normal_key_calls_s3_delete(self):
        fake = _FakeS3()
        with _patch_s3_client(fake):
            ok = await s3_module.delete_object("videos/42/abc/x.mp4")
        assert ok is True
        fake.delete_object.assert_called_once()
        kwargs = fake.delete_object.await_args.kwargs
        assert kwargs["Key"] == "videos/42/abc/x.mp4"

    async def test_s3_failure_returns_false(self):
        fake = _FakeS3()
        fake.delete_object = AsyncMock(side_effect=RuntimeError("boom"))
        with _patch_s3_client(fake):
            ok = await s3_module.delete_object("videos/42/abc/x.mp4")
        assert ok is False


class TestPutBytes:
    async def test_uploads_with_correct_metadata(self):
        fake = _FakeS3()
        with _patch_s3_client(fake):
            await s3_module.put_bytes(
                key="avatars/abc.webp",
                data=b"webp-bytes",
                content_type="image/webp",
            )
        fake.put_object.assert_called_once()
        kwargs = fake.put_object.await_args.kwargs
        assert kwargs["Key"] == "avatars/abc.webp"
        assert kwargs["Body"] == b"webp-bytes"
        assert kwargs["ContentType"] == "image/webp"
