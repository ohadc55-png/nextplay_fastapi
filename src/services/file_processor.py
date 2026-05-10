"""File content extraction — CSV / Excel / PDF / TXT / JSON.

Phase 7 batch 3. Async port of `backend/file_processor.py` extractors.
Vision pipeline (image branch) lives in `src/services/vision.py`
already (Phase 5 batch 9) — this module covers only data files.

All extractors are sync + CPU-heavy (PyMuPDF, pandas). The public API
wraps them in `asyncio.to_thread` so the request handler can `await`
without blocking the event loop. The chat-upload endpoint (next batch)
calls `extract_file_content` for non-image uploads.

Security:
  - Magic-byte validation (`validate_file_content`) blocks attempts to
    pass an .exe disguised as .pdf.
  - CSV-formula sanitization (`_sanitize_cell`) prefixes leading
    `=`/`+`/`-`/`@` so Excel doesn't treat opened content as formula.
  - Hard char caps (PDF 10K, TXT 20K, JSON 15K) bound prompt cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

DATA_EXTENSIONS = frozenset({"csv", "xlsx", "xls", "pdf", "txt", "json"})
IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp"})
SUPPORTED_EXTENSIONS = DATA_EXTENSIONS | IMAGE_EXTENSIONS


_MAGIC_BYTES: dict[str, list[bytes]] = {
    "jpg":  [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "png":  [b"\x89PNG\r\n\x1a\n"],
    "gif":  [b"GIF87a", b"GIF89a"],
    "webp": [b"RIFF"],  # followed by size + "WEBP"
    "pdf":  [b"%PDF"],
    "xlsx": [b"PK\x03\x04"],   # ZIP-based Office format
    "xls":  [b"\xd0\xcf\x11\xe0"],  # OLE2 compound document
    "json": [],  # validated by parsing
    "csv":  [],  # text-based
    "txt":  [],
}


def get_file_extension(filename: str) -> str:
    if not filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def is_image(filename: str) -> bool:
    return get_file_extension(filename) in IMAGE_EXTENSIONS


def is_supported(filename: str) -> bool:
    return get_file_extension(filename) in SUPPORTED_EXTENSIONS


# ---------------------------------------------------------------------------
# Magic-byte validation — content vs. extension
# ---------------------------------------------------------------------------


def _validate_sync(filepath: str, filename: str) -> bool:
    ext = get_file_extension(filename)
    signatures = _MAGIC_BYTES.get(ext, [])
    try:
        with open(filepath, "rb") as f:
            header = f.read(16)
    except OSError as e:
        logger.debug("file validation read error for %s: %s", filepath, e)
        return False

    if not signatures:
        # Text-based formats (csv/txt/json) — block known executable headers.
        dangerous = (b"MZ", b"\x7fELF", b"\xfe\xed\xfa", b"\xca\xfe\xba\xbe", b"#!")
        if any(header.startswith(sig) for sig in dangerous):
            return False
        return True
    return any(header.startswith(sig) for sig in signatures)


async def validate_file_content(filepath: str, filename: str) -> bool:
    """Async wrapper around `_validate_sync`. Off-thread because file IO
    can stutter on slow disks / network mounts."""
    return await asyncio.to_thread(_validate_sync, filepath, filename)


# ---------------------------------------------------------------------------
# Sync extractors (off-thread via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _sanitize_cell(val: Any) -> Any:
    """CSV formula injection guard: prefix a single quote when a cell
    starts with =, +, -, @, |, or tab. Mirrors v1 behavior."""
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "|", "\t"):
        return "'" + val
    return val


def _extract_tabular_sync(
    filepath: str,
    reader_fn: Callable,
    file_type: str,
) -> str:
    """Shared CSV / Excel extraction. Returns a text summary the LLM
    can drop into a prompt."""
    try:
        df = reader_fn(filepath)
        df = df.map(_sanitize_cell)
        summary_lines = [
            f"{file_type} file with {len(df)} rows and {len(df.columns)} columns.",
            f"Columns: {', '.join(df.columns.tolist())}",
            "",
            "First 20 rows:",
            df.head(20).to_string(index=False),
        ]
        if len(df) > 20:
            summary_lines.append(f"\n... and {len(df) - 20} more rows.")
        numeric_cols = df.select_dtypes(include="number").columns
        if len(numeric_cols) > 0:
            summary_lines.append("\nBasic Statistics:")
            summary_lines.append(df[numeric_cols].describe().to_string())
        return "\n".join(summary_lines)
    except (ValueError, KeyError, OSError) as e:
        return f"[Error reading {file_type}: {e}]"


def _extract_csv_sync(filepath: str) -> str:
    import pandas as pd

    return _extract_tabular_sync(filepath, pd.read_csv, "CSV")


def _extract_excel_sync(filepath: str) -> str:
    import pandas as pd

    return _extract_tabular_sync(filepath, pd.read_excel, "Excel")


def _extract_pdf_sync(filepath: str) -> str:
    """PyMuPDF-first; falls back to PyPDF2 if pymupdf isn't importable.
    Mirrors v1 backend/file_processor.py:147-177."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(filepath)
        text_parts: list[str] = []
        try:
            total_pages = len(doc)
            for i, page in enumerate(doc):
                page_text = page.get_text()
                if page_text.strip():
                    text_parts.append(f"--- Page {i + 1} ---\n{page_text}")
                if sum(len(p) for p in text_parts) > 10000:
                    text_parts.append(f"\n[Truncated - {total_pages} total pages]")
                    break
        finally:
            doc.close()
        text = "\n".join(text_parts)
        return text if text.strip() else "[PDF has no extractable text - may be scanned/image-based]"
    except ImportError:
        try:
            from PyPDF2 import PdfReader

            reader = PdfReader(filepath)
            text_parts = []
            total_pages = len(reader.pages)
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_parts.append(f"--- Page {i + 1} ---\n{page_text}")
                if sum(len(p) for p in text_parts) > 10000:
                    text_parts.append(f"\n[Truncated - {total_pages} total pages]")
                    break
            text = "\n".join(text_parts)
            return text if text.strip() else "[PDF has no extractable text]"
        except (ImportError, OSError, ValueError) as e:
            return f"[Error reading PDF: {e}]"
    except (OSError, ValueError) as e:
        return f"[Error reading PDF: {e}]"


def _extract_text_sync(filepath: str) -> str:
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read(20000)
        if os.path.getsize(filepath) > 20000:
            content += "\n[Truncated - file is larger than 20KB]"
        return content
    except (OSError, UnicodeDecodeError) as e:
        return f"[Error reading text file: {e}]"


def _extract_json_sync(filepath: str) -> str:
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
        formatted = json.dumps(data, indent=2, ensure_ascii=False)
        if len(formatted) > 15000:
            formatted = formatted[:15000] + "\n[Truncated]"
        return formatted
    except (json.JSONDecodeError, ValueError, OSError) as e:
        return f"[Error reading JSON: {e}]"


def _extract_sync(filepath: str, filename: str) -> str | None:
    """Sync dispatcher. Returns None for image files (caller should
    route to vision.describe_basketball_image instead)."""
    ext = get_file_extension(filename)
    if ext == "csv":
        return _extract_csv_sync(filepath)
    if ext in ("xlsx", "xls"):
        return _extract_excel_sync(filepath)
    if ext == "pdf":
        return _extract_pdf_sync(filepath)
    if ext == "txt":
        return _extract_text_sync(filepath)
    if ext == "json":
        return _extract_json_sync(filepath)
    if ext in IMAGE_EXTENSIONS:
        return None  # vision pipeline handles images
    return f"[Unsupported file type: {ext}]"


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------


async def extract_file_content(filepath: str, filename: str) -> str | None:
    """Extract readable content from a data file. Returns None for
    images (caller is expected to route those to the Vision pipeline).
    Off-thread so PyMuPDF / pandas don't stall the event loop."""
    return await asyncio.to_thread(_extract_sync, filepath, filename)


__all__ = [
    "DATA_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "extract_file_content",
    "get_file_extension",
    "is_image",
    "is_supported",
    "validate_file_content",
]
