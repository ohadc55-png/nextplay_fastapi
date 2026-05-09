"""Admin email-broadcast schemas (`/emails/*`)."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class EmailPreviewRequest(BaseModel):
    template: str
    language: str | None = "en"
    context: dict | None = None


class EmailTestSendRequest(BaseModel):
    template: str
    to_email: EmailStr
    context: dict | None = None


class EmailBroadcastRequest(BaseModel):
    """Send a broadcast to a saved mailing list."""

    list_id: int
    template: str
    subject: str = Field(min_length=1)
    context: dict | None = None


class MailingListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = ""


class MailingListMemberAdd(BaseModel):
    user_id: int


__all__ = [
    "EmailPreviewRequest",
    "EmailTestSendRequest",
    "EmailBroadcastRequest",
    "MailingListCreate",
    "MailingListMemberAdd",
]
