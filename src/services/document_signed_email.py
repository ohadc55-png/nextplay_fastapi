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

from src.core.database import AsyncSessionLocal
from src.models.document_campaigns import DocumentCampaign
from src.models.document_templates import DocumentTemplate
from src.models.organizations import Organization
from src.services import s3
from src.services.email import schedule_email
from src.services.email_templates import render as render_email

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

    # Look up the template + org names so the branded template can render
    # them. Failures fall back to generic strings — we never want this
    # confirmation email to fail because of a slow lookup.
    template_name = ""
    org_name = ""
    try:
        async with AsyncSessionLocal() as session:
            campaign = await session.get(DocumentCampaign, delivery.campaign_id)
            if campaign:
                template = await session.get(DocumentTemplate, campaign.template_id)
                if template:
                    template_name = template.name
            org = await session.get(Organization, delivery.organization_id)
            if org:
                org_name = org.name
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("[signed-email] lookup failed for delivery %d: %s", delivery.id, e)

    recipient = delivery.recipient_name or "הורה"
    subject, html, text = render_email(
        "document_signed",
        language="he",
        context={
            "recipient_name": recipient,
            "template_name": template_name or "המסמך",
            "organization_name": org_name or "הארגון",
            "download_url": download_url,
            "cta_url": download_url if download_url else None,
            "cta_label_he": "הורדת המסמך",
            "cta_label_en": "Download document",
        },
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
