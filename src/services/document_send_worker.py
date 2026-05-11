"""Per-delivery dispatch worker — Phase 2.4.

`dispatch_delivery(delivery_id)` runs as a FastAPI BackgroundTask: opens
its own AsyncSessionLocal (the request session is already closed by the
time it fires), reads the delivery row, tries the campaign's channels in
priority order (`["email", "sms"]` / `["sms", "email"]`), and flips the
status to SENT_EMAIL / SENT_SMS / FAILED.

Idempotent: re-running on a row already at SENT_* skips the send. The
campaign counters update atomically inside the worker's transaction.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.models.document_campaigns import DocumentCampaign
from src.models.document_deliveries import DocumentDelivery
from src.models.document_templates import DocumentTemplate
from src.models.organizations import Organization
from src.services.email import send_email_now
from src.services.sms import get_sms_provider

logger = logging.getLogger(__name__)


def _signing_url(token: str) -> str:
    """Build the public link the parent will click."""
    base = (settings.APP_BASE_URL or settings.BASE_URL or "http://127.0.0.1:5050").rstrip("/")
    return f"{base}/sign/{token}"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _send_sms_for(delivery: DocumentDelivery, template: DocumentTemplate) -> bool:
    """Send via mock SMS. Returns True on success."""
    if not delivery.recipient_phone:
        return False
    url = _signing_url(delivery.unique_token)
    body = (
        f"שלום, יש לך מסמך לחתימה ({template.name})"
        + (f" עבור {delivery.recipient_name}" if delivery.recipient_name else "")
        + f":\n{url}\nNEXTPLAY"
    )
    provider = get_sms_provider()
    result = await provider.send(delivery.recipient_phone, body)
    return bool(result.success)


async def _send_email_for(
    session: AsyncSession,
    delivery: DocumentDelivery,
    template: DocumentTemplate,
    org: Organization | None,
) -> bool:
    """Send the invite email via the existing email service. Returns True
    on success. Console mode just logs to stdout — that's the dev/test path."""
    if not delivery.recipient_email:
        return False
    url = _signing_url(delivery.unique_token)
    org_name = (org.name if org else "") or "הארגון"
    recipient = delivery.recipient_name or "הורה"
    subject = f"מסמך לחתימה — {template.name}"
    text = (
        f"שלום {recipient},\n\n"
        f"{org_name} שלח לך מסמך לחתימה: {template.name}.\n\n"
        f"קישור לחתימה (תקף ל-{template.default_expiry_days or 30} ימים):\n{url}\n\n"
        "תודה,\nNEXTPLAY"
    )
    html = (
        '<div dir="rtl" style="font-family: Arial, sans-serif; line-height:1.5;">'
        f"<p>שלום {recipient},</p>"
        f"<p>{org_name} שלח לך מסמך לחתימה: <strong>{template.name}</strong>.</p>"
        f'<p><a href="{url}">לחץ כאן לחתימה</a></p>'
        f'<p class="muted">הקישור תקף ל-{template.default_expiry_days or 30} ימים.</p>'
        "<p>תודה,<br>NEXTPLAY</p></div>"
    )
    result = await send_email_now(
        session,
        user_id=None,
        to_email=delivery.recipient_email,
        subject=subject,
        html=html,
        text=text,
        template="document.invite",
        language="he",
        kind="transactional",
    )
    return bool(result and result.get("status") == "sent")


async def dispatch_delivery(delivery_id: int) -> None:
    """Background task entry point. Reads the delivery, picks channels in
    priority order, and updates status. Never raises — errors are recorded
    in `failed_reason`."""
    async with AsyncSessionLocal() as session:
        delivery = await session.get(DocumentDelivery, delivery_id)
        if delivery is None:
            logger.warning("[send-worker] delivery %d not found", delivery_id)
            return
        if delivery.delivery_status not in ("PENDING",):
            return  # already dispatched or failed previously

        campaign = await session.get(DocumentCampaign, delivery.campaign_id)
        if campaign is None:
            delivery.delivery_status = "FAILED"
            delivery.failed_reason = "campaign not found"
            await session.commit()
            return
        template = await session.get(DocumentTemplate, campaign.template_id)
        org = await session.get(Organization, delivery.organization_id)
        if template is None:
            delivery.delivery_status = "FAILED"
            delivery.failed_reason = "template not found"
            await session.commit()
            return

        channels = list(campaign.delivery_channels or ["sms"])
        last_error: str | None = None
        for channel in channels:
            try:
                if channel == "sms":
                    ok = await _send_sms_for(delivery, template)
                    if ok:
                        delivery.delivery_status = "SENT_SMS"
                        delivery.channel_used = "SMS"
                        delivery.sent_at = _now()
                        last_error = None
                        break
                elif channel == "email":
                    ok = await _send_email_for(session, delivery, template, org)
                    if ok:
                        delivery.delivery_status = "SENT_EMAIL"
                        delivery.channel_used = "EMAIL"
                        delivery.sent_at = _now()
                        last_error = None
                        break
            except Exception as e:  # pragma: no cover — defensive
                last_error = f"{channel}: {e}"

        if delivery.delivery_status == "PENDING":
            delivery.delivery_status = "FAILED"
            delivery.failed_reason = last_error or "no working channel"
            campaign.total_failed = (campaign.total_failed or 0) + 1

        await session.commit()


__all__ = ["dispatch_delivery"]
