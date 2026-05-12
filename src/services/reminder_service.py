"""Document-reminders cron — Phase 2.6b.

The reminder cron looks at every PENDING/OPENED DocumentDelivery and decides
whether it's due for a nudge. "Due" means:

  - parent has not signed yet (document_status in NOT_OPENED/OPENED/FILLED)
  - delivery is not expired
  - campaign.reminder_after_days is set (opt-in per campaign)
  - days_since_last_contact >= campaign.reminder_after_days
       (last_reminder_sent_at if any, else sent_at)
  - reminders_sent_count < MAX_REMINDERS

Safety rails (designed after the local Resend over-sending incident):

  1. MAX_REMINDERS=2 hard cap per delivery, regardless of opt-in.
  2. Per-run cap MAX_PER_RUN=500 — even if 5000 deliveries are due, we
     process the oldest 500 and the rest wait for next run.
  3. dry_run mode returns the list of due deliveries without sending or
     mutating any rows. Used for the smoke endpoint.
  4. EMAIL_MODE=console + SMS mock provider work as-is: nothing leaves
     the box unless prod is wired up.

Idempotent: re-running mid-cron is safe; the worker rechecks the row's
state inside its own session before sending.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import AsyncSessionLocal
from src.models.document_campaigns import DocumentCampaign
from src.models.document_deliveries import DocumentDelivery
from src.models.document_templates import DocumentTemplate
from src.models.organizations import Organization
from src.services.email import send_email_now
from src.services.email_templates import render as render_email
from src.services.sms import get_sms_provider

logger = logging.getLogger(__name__)


MAX_REMINDERS = 2
MAX_PER_RUN = 500


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _signing_url(token: str) -> str:
    from src.core.config import settings
    base = (settings.APP_BASE_URL or settings.BASE_URL or "http://127.0.0.1:5050").rstrip("/")
    return f"{base}/sign/{token}"


async def find_due_reminders(
    session: AsyncSession,
    *,
    limit: int = MAX_PER_RUN,
) -> list[tuple[DocumentDelivery, DocumentCampaign]]:
    """Returns (delivery, campaign) pairs that are due for a reminder right
    now. Bounded by `limit` (default MAX_PER_RUN)."""
    now = _now()
    stmt = (
        select(DocumentDelivery, DocumentCampaign)
        .join(DocumentCampaign, DocumentCampaign.id == DocumentDelivery.campaign_id)
        .where(DocumentDelivery.document_status.in_(("NOT_OPENED", "OPENED", "FILLED")))
        .where(DocumentDelivery.delivery_status.in_(("SENT_SMS", "SENT_EMAIL", "DELIVERED")))
        .where(DocumentDelivery.expires_at > now)
        .where(DocumentDelivery.reminders_sent_count < MAX_REMINDERS)
        .where(DocumentCampaign.reminder_after_days.isnot(None))
        # Sort oldest-first so we never starve a long-waiting parent.
        .order_by(DocumentDelivery.sent_at.asc())
        .limit(limit * 2)  # over-fetch then filter the "days since" check in Python
    )
    rows = (await session.execute(stmt)).all()

    due: list[tuple[DocumentDelivery, DocumentCampaign]] = []
    for delivery, campaign in rows:
        ref_time = delivery.last_reminder_sent_at or delivery.sent_at
        if ref_time is None:
            continue
        threshold = ref_time + timedelta(days=campaign.reminder_after_days or 7)
        if threshold <= now:
            due.append((delivery, campaign))
        if len(due) >= limit:
            break
    return due


async def _send_reminder_sms(delivery: DocumentDelivery, template: DocumentTemplate) -> bool:
    if not delivery.recipient_phone:
        return False
    url = _signing_url(delivery.unique_token)
    body = (
        f"תזכורת: יש לך מסמך לחתימה ({template.name})"
        + (f" עבור {delivery.recipient_name}" if delivery.recipient_name else "")
        + f":\n{url}\nNEXTPLAY"
    )
    result = await get_sms_provider().send(delivery.recipient_phone, body)
    return bool(result.success)


async def _send_reminder_email(
    session: AsyncSession,
    delivery: DocumentDelivery,
    template: DocumentTemplate,
    org: Organization | None,
) -> bool:
    if not delivery.recipient_email:
        return False
    url = _signing_url(delivery.unique_token)
    org_name = (org.name if org else "") or "הארגון"
    recipient = delivery.recipient_name or "הורה"
    subject, html, text = render_email(
        "document_reminder",
        language="he",
        context={
            "recipient_name": recipient,
            "template_name": template.name,
            "organization_name": org_name,
            "sign_url": url,
            "expiry_days": template.default_expiry_days or 30,
            "cta_url": url,
            "cta_label_he": "לחתימה",
            "cta_label_en": "Sign now",
        },
    )
    result = await send_email_now(
        session, user_id=None,
        to_email=delivery.recipient_email,
        subject=subject, html=html, text=text,
        template="document.reminder", language="he", kind="transactional",
    )
    return bool(result and result.get("status") == "sent")


async def dispatch_reminder(delivery_id: int) -> bool:
    """BackgroundTask entry. Rechecks the delivery state in its own session
    before sending — protects against double-fires between cron + manual.

    Returns True if a reminder was actually sent.
    """
    async with AsyncSessionLocal() as session:
        delivery = await session.get(DocumentDelivery, delivery_id)
        if delivery is None:
            return False
        if delivery.document_status not in ("NOT_OPENED", "OPENED", "FILLED"):
            return False
        if delivery.reminders_sent_count >= MAX_REMINDERS:
            return False
        campaign = await session.get(DocumentCampaign, delivery.campaign_id)
        template = await session.get(DocumentTemplate, campaign.template_id) if campaign else None
        org = await session.get(Organization, delivery.organization_id)
        if template is None:
            return False

        # Pick the channel that already worked once; fall back to the other.
        channels = list(campaign.delivery_channels or ["sms"]) if campaign else ["sms"]
        sent = False
        for channel in channels:
            try:
                if channel == "sms" and await _send_reminder_sms(delivery, template):
                    sent = True
                    break
                if channel == "email" and await _send_reminder_email(
                    session, delivery, template, org,
                ):
                    sent = True
                    break
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("[reminder] %s failed: %s", channel, e)

        if sent:
            delivery.last_reminder_sent_at = _now()
            delivery.reminders_sent_count = (delivery.reminders_sent_count or 0) + 1
            await session.commit()
        return sent


async def run_reminders(
    *,
    dry_run: bool = False,
    limit: int = MAX_PER_RUN,
) -> dict:
    """Top-level entry the cron endpoint calls.

    dry_run=True returns the list of would-be reminders without touching
    any state. Use this to verify config + scope before flipping the live
    cron in prod.
    """
    async with AsyncSessionLocal() as session:
        due = await find_due_reminders(session, limit=limit)

    summary = {
        "due_count": len(due),
        "max_per_run": limit,
        "dry_run": dry_run,
        "sent": 0,
        "failed": 0,
    }
    if dry_run:
        summary["preview"] = [
            {
                "delivery_id": d.id,
                "campaign_id": d.campaign_id,
                "recipient_name": d.recipient_name,
                "channel": d.channel_used,
                "sent_at": d.sent_at.isoformat() if d.sent_at else None,
                "last_reminder_sent_at": (
                    d.last_reminder_sent_at.isoformat() if d.last_reminder_sent_at else None
                ),
                "reminders_sent_count": d.reminders_sent_count or 0,
            }
            for d, _c in due
        ]
        return summary

    for delivery, _campaign in due:
        ok = await dispatch_reminder(delivery.id)
        if ok:
            summary["sent"] += 1
        else:
            summary["failed"] += 1
    return summary


__all__ = [
    "MAX_PER_RUN",
    "MAX_REMINDERS",
    "dispatch_reminder",
    "find_due_reminders",
    "run_reminders",
]
