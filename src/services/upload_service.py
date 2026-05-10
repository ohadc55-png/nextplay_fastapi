"""Local file storage helper — `data/uploads/` directory.

Phase 7 batch 4. Mirrors v1 `backend/services/upload_service.py` for
the simple cases the chat-upload endpoint needs:
  - sanitize the filename
  - dedupe collisions (`name.ext`, `name_1.ext`, ...)
  - write bytes to disk
  - delete on cleanup
S3 / videos paths are out of scope here (Phase 6 covers them via the
async S3 service).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from src.services.file_processor import is_supported, validate_file_content

logger = logging.getLogger(__name__)


# Repo-root/data/uploads — same convention as v1 (data/uploads/ lives
# next to data/coach.db).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
UPLOAD_DIR = str(_PROJECT_ROOT / "data" / "uploads")


def _ensure_dir() -> None:
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def _sanitize_filename(name: str) -> str:
    """ASCII-safe filename for the local disk. Keep extension verbatim
    (lowercased); strip everything else to alnum / dash / underscore."""
    parts = (name or "").rsplit(".", 1)
    ext = parts[1].lower() if len(parts) > 1 else "bin"
    base = re.sub(r"[^a-zA-Z0-9_\-]", "_", parts[0])
    base = re.sub(r"_+", "_", base).strip("_")
    if not base:
        base = "upload"
    return f"{base[:100]}.{ext}"


def _unique_path(directory: str, filename: str) -> str:
    """Pick a non-colliding absolute path inside `directory`."""
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(filename)
    n = 1
    while True:
        candidate = os.path.join(directory, f"{base}_{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _write_sync(filepath: str, data: bytes) -> None:
    with open(filepath, "wb") as f:
        f.write(data)


async def save_upload_bytes(
    *,
    user_id: int,
    filename: str,
    data: bytes,
    validate_supported: bool = True,
) -> tuple[str, str]:
    """Save bytes to `data/uploads/<user_id>/<safe_name>`. Returns
    `(safe_name, abs_path)`. Raises ValueError on unsupported extensions
    or content-vs-extension mismatch.

    Multi-tenancy: each user gets their own subdirectory so a path-traversal
    bug in cleanup logic can't reach other coaches' files.
    """
    if validate_supported and not is_supported(filename):
        raise ValueError(f"Unsupported file type: {filename}")

    user_dir = os.path.join(UPLOAD_DIR, str(int(user_id)))
    os.makedirs(user_dir, exist_ok=True)

    safe = _sanitize_filename(filename)
    target = _unique_path(user_dir, safe)

    await asyncio.to_thread(_write_sync, target, data)

    if validate_supported:
        # Magic-byte check happens AFTER write because the validator
        # reads the first 16 bytes from disk. If it fails, reverse the write.
        if not await validate_file_content(target, filename):
            try:
                os.remove(target)
            except OSError:
                pass
            raise ValueError(f"File '{filename}' content doesn't match its extension")

    return os.path.basename(target), target


async def delete_upload(filepath: str) -> bool:
    """Best-effort delete. Returns False on failure but doesn't raise."""
    try:
        await asyncio.to_thread(os.remove, filepath)
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        logger.warning("[upload] could not delete %s: %s", filepath, e)
        return False


__all__ = [
    "UPLOAD_DIR",
    "delete_upload",
    "save_upload_bytes",
]
