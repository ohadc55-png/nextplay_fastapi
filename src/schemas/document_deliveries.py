"""Pydantic shapes for the public signing flow (Phase 2.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel

DeliveryStatus = Literal[
    "PENDING", "SENT_EMAIL", "SENT_SMS", "DELIVERED", "BOUNCED", "FAILED"
]
DocumentStatus = Literal[
    "NOT_OPENED", "OPENED", "FILLED", "SIGNED", "DECLINED", "EXPIRED"
]
SignatureMethod = Literal["DRAWN", "TYPED"]


class DocumentDeliveryOut(ORMModel):
    """Internal admin/dashboard view of a delivery. NOT exposed to the
    parent (the public page only sees a curated subset rendered server-side)."""

    id: int
    campaign_id: int
    organization_id: int
    player_id: int
    player_contact_id: int | None = None
    recipient_name: str
    recipient_email: str | None = None
    # NB: phone is decrypted at snapshot. We deliberately don't expose
    # it via this schema. If a future admin endpoint needs it, log a
    # `player.contact.read` audit row first.
    unique_token: str
    delivery_status: str
    channel_used: str | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    failed_reason: str | None = None
    document_status: str
    opened_at: datetime | None = None
    signed_at: datetime | None = None
    signature_method: str | None = None
    final_pdf_url: str | None = None  # S3 key
    expires_at: datetime
    last_reminder_sent_at: datetime | None = None
    reminders_sent_count: int
    created_at: datetime
    updated_at: datetime


class OTPRequest(BaseModel):
    """POST /sign/{token}/otp/request — parent submits their phone."""

    phone: str = Field(min_length=6, max_length=20)


class OTPVerify(BaseModel):
    """POST /sign/{token}/otp/verify — parent submits the 6-digit code."""

    code: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")


class SignatureSubmit(BaseModel):
    """POST /sign/{token}/submit — final signing payload.

    Either `signature_image_base64` (for DRAWN) or `typed_signature` (for
    TYPED) is required when the template requires signature; both being
    None is only valid for form-fill-only templates.
    """

    form_response: dict
    signature_method: SignatureMethod | None = None
    signature_image_base64: str | None = None
    typed_signature: str | None = Field(default=None, max_length=200)


__all__ = [
    "DeliveryStatus",
    "DocumentDeliveryOut",
    "DocumentStatus",
    "OTPRequest",
    "OTPVerify",
    "SignatureMethod",
    "SignatureSubmit",
]
