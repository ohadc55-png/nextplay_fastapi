"""Final PDF generation — Phase 2.3.

After a parent submits a signed form, we burn:
  - their `form_response` values onto each field's PDF coordinates,
  - the signature image (or rendered typed signature text) inside each
    signature zone,
  - a small ASCII footer with the delivery id + timestamp (Hebrew shaping
    in PDF requires a bundled font; deferred to Part B).

Result is saved to S3 at `org_{org_id}/signed/{delivery_id}.pdf`. The
returned value is the S3 KEY (not URL) — call sites resolve it via
`s3.presign_get(...)`.

PyMuPDF (`fitz`) is the only renderer; it's already in requirements.txt.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.services import s3

if TYPE_CHECKING:
    from src.models.document_deliveries import DocumentDelivery
    from src.models.document_templates import DocumentTemplate

logger = logging.getLogger(__name__)


def _decode_signature_data_url(data_url: str) -> bytes:
    """Accept either a raw base64 string or a `data:image/png;base64,...`
    URL (which is what HTMLCanvasElement.toDataURL produces)."""
    if not data_url:
        return b""
    if "," in data_url and data_url.startswith("data:"):
        _header, _, b64 = data_url.partition(",")
    else:
        b64 = data_url
    try:
        return base64.b64decode(b64)
    except Exception as e:
        logger.warning("[pdf] signature base64 decode failed: %s", e)
        return b""


async def generate_final(
    *,
    template: DocumentTemplate,
    delivery: DocumentDelivery,
    form_response: dict,
    signature_image_base64: str | None,
    typed_signature: str | None,
) -> str:
    """Render the final signed PDF and upload to S3. Returns the S3 key.

    The caller has already validated:
      - All required form fields are present.
      - signature method matches what's expected.
    This function just paints + saves.
    """
    # PyMuPDF is heavy; import lazily so unrelated code paths don't pay.
    import fitz

    original_bytes = await s3.get_bytes(template.uploaded_file_url)
    doc = fitz.open(stream=original_bytes, filetype="pdf")
    try:
        # ---- Overlay form fields ----
        for field in template.form_fields or []:
            value = form_response.get(field["id"])
            if value is None:
                continue
            page_idx = int(field.get("page", 1)) - 1
            if page_idx < 0 or page_idx >= doc.page_count:
                continue
            page = doc[page_idx]
            ftype = field.get("type", "text")
            text_value = _format_field_value(ftype, value)
            page.insert_text(
                point=(int(field["x"]), int(field["y"]) + int(field.get("height", 16)) - 4),
                text=text_value,
                fontsize=12,
                color=(0, 0, 0),
            )

        # ---- Embed signature ----
        sig_bytes = _decode_signature_data_url(signature_image_base64 or "")
        for zone in template.signature_zones or []:
            page_idx = int(zone.get("page", 1)) - 1
            if page_idx < 0 or page_idx >= doc.page_count:
                continue
            page = doc[page_idx]
            rect = fitz.Rect(
                int(zone["x"]),
                int(zone["y"]),
                int(zone["x"]) + int(zone["width"]),
                int(zone["y"]) + int(zone["height"]),
            )
            if sig_bytes:
                page.insert_image(rect, stream=sig_bytes)
            elif typed_signature:
                # Render the typed signature as italic-styled text inside
                # the rect. (PyMuPDF base fonts include latin only — for
                # Hebrew typed signatures the result will look ASCII-ish
                # until Part B's Heebo bundle.)
                page.insert_textbox(
                    rect,
                    typed_signature,
                    fontsize=min(int(zone["height"]) - 6, 32),
                    align=1,  # center
                    color=(0, 0, 0.5),
                )

        # ---- ASCII footer on the last page ----
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        footer = (
            f"Signed digitally {now}  |  delivery_id={delivery.id}  |  NEXTPLAY"
        )
        last_page = doc[-1]
        last_page.insert_text(
            point=(40, last_page.rect.height - 24),
            text=footer,
            fontsize=8,
            color=(0.4, 0.4, 0.4),
        )

        final_bytes: bytes = doc.tobytes()
    finally:
        doc.close()

    key = f"org_{template.organization_id}/signed/{delivery.id}.pdf"
    await s3.put_bytes(key=key, data=final_bytes, content_type="application/pdf")
    return key


def _format_field_value(field_type: str, value) -> str:
    """Coerce a form value to the string we'll burn into the PDF."""
    if field_type == "checkbox":
        return "✓" if value else ""
    if value is None:
        return ""
    return str(value)


__all__ = ["generate_final"]
