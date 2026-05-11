"""Pydantic shapes for /org/api/messages/* (Phase 2.5, lands in 2.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel
from src.schemas.document_campaigns import DeliveryChannel, RecipientFilter

MessageStatus = Literal["DRAFT", "SCHEDULED", "SENDING", "SENT", "CANCELLED"]
MessageDeliveryStatus = Literal["PENDING", "SENT", "DELIVERED", "BOUNCED", "FAILED"]


class MessageCreate(BaseModel):
    """POST /org/api/messages — body for creating + scheduling a message."""

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    recipient_filter: RecipientFilter
    delivery_channels: list[DeliveryChannel] = Field(min_length=1)
    scheduled_at: datetime | None = None


class MessageOut(ORMModel):
    id: int
    organization_id: int
    subject: str
    body: str
    recipient_filter: dict
    delivery_channels: list[str]
    scheduled_at: datetime | None = None
    status: str
    total_recipients: int
    total_delivered: int
    total_failed: int
    sent_by_user_id: int | None = None
    sent_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MessageDeliveryOut(ORMModel):
    id: int
    message_id: int
    organization_id: int
    player_id: int
    player_contact_id: int | None = None
    recipient_email: str | None = None
    # phone deliberately omitted — same rationale as DocumentDeliveryOut.
    channel_used: str | None = None
    delivery_status: str
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    failed_reason: str | None = None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "MessageCreate",
    "MessageDeliveryOut",
    "MessageDeliveryStatus",
    "MessageOut",
    "MessageStatus",
]
