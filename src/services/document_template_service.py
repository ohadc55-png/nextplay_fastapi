"""DocumentTemplate upload + preview service — Phase 2.2.

Responsibilities (single-file because every method shares the same s3 +
fitz dependencies):

- `process_uploaded_file`: validate type (PDF or DOCX) by magic bytes, size
  (≤ 10 MB), upload to S3 via `put_bytes`, return the saved
  DocumentTemplate row. Caller persists.
- `render_template_preview`: download the template's bytes from S3 and
  render the requested page as PNG at 150 DPI using PyMuPDF. DOCX files
  in Part A return a placeholder PNG saying "preview not available yet"
  — full DOCX rendering would require LibreOffice / a headless office
  process, deferred to Part B.

The service never opens a session — the caller does. We just return the
in-memory ORM row, ready to be added + flushed.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from src.core.exceptions import ValidationError
from src.models.document_templates import DocumentTemplate
from src.services import s3

if TYPE_CHECKING:
    from fastapi import UploadFile

logger = logging.getLogger(__name__)


MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
PDF_MAGIC = b"%PDF-"
# DOCX is a zip with a specific content-types entry. Detect by ZIP magic
# (`PK\x03\x04`) — full content-types validation is overkill for Part A.
ZIP_MAGIC = b"PK\x03\x04"


def _detect_file_type(head: bytes) -> str:
    """Return 'PDF' | 'DOCX'. Raises ValidationError on anything else."""
    if head.startswith(PDF_MAGIC):
        return "PDF"
    if head.startswith(ZIP_MAGIC):
        # We trust the ZIP for now — a corrupt DOCX still gets caught
        # downstream when fitz / docx parsers fail.
        return "DOCX"
    raise ValidationError(
        "Unsupported file type. Upload a PDF or DOCX.",
        code="unsupported_file_type",
    )


async def process_uploaded_file(
    file: UploadFile,
    *,
    organization_id: int,
    name: str,
    description: str | None,
    category: str,
    requires_signature: bool,
    created_by_user_id: int | None,
) -> DocumentTemplate:
    """Validate + upload + return an unsaved DocumentTemplate ORM row.

    The caller adds + flushes; doing it here would tightly couple the
    service to a session, making testing harder.
    """
    body = await file.read()
    if not body:
        raise ValidationError("Uploaded file is empty.", code="empty_file")
    if len(body) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
            code="file_too_large",
        )
    file_type = _detect_file_type(body[:8])

    ext = "pdf" if file_type == "PDF" else "docx"
    content_type = (
        "application/pdf" if file_type == "PDF"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    key = f"org_{organization_id}/templates/{uuid.uuid4().hex[:12]}.{ext}"

    # Upload — surfaces as 500 if S3 errors. In local dev with no AWS
    # creds, callers should mock s3.put_bytes or run with S3 emulation.
    await s3.put_bytes(key=key, data=body, content_type=content_type)

    return DocumentTemplate(
        organization_id=organization_id,
        name=name.strip(),
        description=(description or None),
        category=category,
        uploaded_file_url=key,  # KEY, not URL — see schema comment
        uploaded_file_type=file_type,
        uploaded_file_size=len(body),
        requires_signature=requires_signature,
        created_by_user_id=created_by_user_id,
    )


async def render_template_preview(
    template: DocumentTemplate,
    *,
    page: int = 1,
    dpi: int = 150,
) -> bytes:
    """Return a PNG snapshot of a single template page.

    DOCX returns a placeholder PNG today — the field-marking UI hides
    the canvas overlay for DOCX templates (no fitz preview available).
    """
    if template.uploaded_file_type == "DOCX":
        return _docx_placeholder_png()

    body = await s3.get_bytes(template.uploaded_file_url)

    # PyMuPDF is imported lazily so the rest of the codebase doesn't
    # pay the import cost (it pulls in a large C extension).
    import fitz

    doc = fitz.open(stream=body, filetype="pdf")
    try:
        # Clamp page index — 1-based input, fitz is 0-based.
        page_idx = max(1, min(page, doc.page_count)) - 1
        pdf_page = doc[page_idx]
        # 72 DPI is the PDF unit; scale by dpi/72.
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = pdf_page.get_pixmap(matrix=matrix, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


# Tiny pre-rendered "preview not available" PNG (1x1 grey). The UI
# checks template.uploaded_file_type and skips canvas overlay for
# DOCX, so the actual visual doesn't matter — this just gives the
# endpoint something to return.
_DOCX_PLACEHOLDER_PNG: bytes | None = None


def _docx_placeholder_png() -> bytes:
    global _DOCX_PLACEHOLDER_PNG
    if _DOCX_PLACEHOLDER_PNG is None:
        # Generate once on first call so we don't pay PyMuPDF startup
        # at import time. 612x792 = US Letter @ 72 DPI for similar dims.
        import fitz

        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text(
            (60, 100),
            "DOCX preview is not available yet.",
            fontsize=18,
        )
        page.insert_text(
            (60, 130),
            "Field marking is disabled for DOCX templates in this release.",
            fontsize=12,
        )
        pix = page.get_pixmap(alpha=False)
        _DOCX_PLACEHOLDER_PNG = pix.tobytes("png")
        doc.close()
    return _DOCX_PLACEHOLDER_PNG


__all__ = [
    "MAX_UPLOAD_BYTES",
    "process_uploaded_file",
    "render_template_preview",
]
