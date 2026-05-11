"""Confirmation email after a successful signature — Phase 2.3.

Email backend doesn't currently support real attachments (see
src/services/email.py). For Part A we include a **7-day presigned S3
link** to the final PDF in the email body instead. Part B can replace
this with a true attachment via Resend's attachments API.

Failure policy: never raise — the parent has already seen the success
page; an email send failure shouldn't 500 the (already-sent) HTTP
response. Errors are logged via the existing email_log pipeline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.services import s3
from src.services.email import schedule_email

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

    from src.models.document_deliveries import DocumentDelivery

logger = logging.getLogger(__name__)


async def send_signed_document_email(
    *,
    delivery: DocumentDelivery,
    background: BackgroundTasks,
) -> None:
    """Queue a confirmation email with a presigned download link.

    No-op when `delivery.recipient_email` is empty (e.g. SMS-only flows) —
    we still consider the delivery successful in that case.
    """
    if not delivery.recipient_email:
        return
    if not delivery.final_pdf_url:
        logger.warning(
            "[signed-email] delivery %d has no final_pdf_url; skipping email",
            delivery.id,
        )
        return

    try:
        download_url = await s3.presign_get(delivery.final_pdf_url, ttl_seconds=604800)
    except Exception as e:  # pragma: no cover — S3 misconfig
        logger.warning(
            "[signed-email] presign failed for delivery %d: %s", delivery.id, e
        )
        download_url = ""

    recipient = delivery.recipient_name or ""
    subject = "אישור — המסמך נחתם"
    text = (
        f"שלום {recipient},\n\n"
        f"המסמך שחתמת נשמר בהצלחה.\n\n"
        + (f"קישור להורדה (תקף ל-7 ימים):\n{download_url}\n\n" if download_url else "")
        + "תודה,\nNEXTPLAY"
    )
    html = (
        '<div dir="rtl" style="font-family: Arial, sans-serif; line-height:1.5;">'
        f"<p>שלום {recipient},</p>"
        "<p>המסמך שחתמת נשמר בהצלחה.</p>"
        + (
            f'<p><a href="{download_url}">קישור להורדה (תקף ל-7 ימים)</a></p>'
            if download_url else ""
        )
        + "<p>תודה,<br>NEXTPLAY</p></div>"
    )

    schedule_email(
        background,
        user_id=None,  # parent isn't a user
        to_email=delivery.recipient_email,
        subject=subject,
        html=html,
        text=text,
        template="document.signed",
        language="he",
        kind="transactional",
    )


__all__ = ["send_signed_document_email"]
