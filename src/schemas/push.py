"""Web Push subscription schemas."""

from __future__ import annotations

from pydantic import BaseModel


class PushKeysSchema(BaseModel):
    """Web Push API subscription.keys field."""

    p256dh: str
    auth: str


class PushSubscribeRequest(BaseModel):
    """`/api/push/subscribe` body. Mirrors the browser's PushSubscription.toJSON()."""

    endpoint: str
    keys: PushKeysSchema
    user_agent: str | None = None


class PushPreferencesUpdate(BaseModel):
    """`/api/push/preferences`."""

    push_enabled: bool | None = None
    push_quiet_start: int | None = None  # 0-23
    push_quiet_end: int | None = None  # 0-23
    timezone: str | None = None


class PushTestRequest(BaseModel):
    title: str | None = "Test"
    body: str | None = "Hello from NEXTPLAY"


class VapidKeyResponse(BaseModel):
    public_key: str


__all__ = [
    "PushKeysSchema",
    "PushSubscribeRequest",
    "PushPreferencesUpdate",
    "PushTestRequest",
    "VapidKeyResponse",
]
