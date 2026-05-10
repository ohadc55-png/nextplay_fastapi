"""Web Push subscription schemas.

Wire format mirrors v1.0-flask `backend/api/push_routes.py` exactly so the
existing browser code (sw.js + push subscribe flow) keeps working.
"""

from __future__ import annotations

from pydantic import BaseModel


class PushKeysSchema(BaseModel):
    """Web Push API `subscription.keys` field (browser-emitted)."""

    p256dh: str
    auth: str


class PushSubscribeRequest(BaseModel):
    """`POST /api/push/subscribe` body. Mirrors what the browser hands us
    via `subscription.toJSON()`. `timezone` is captured for scheduling so
    we can send pushes during local 10-12 (PUSH_MORNING_START/END)."""

    endpoint: str
    keys: PushKeysSchema
    timezone: str | None = None  # IANA name; optional


class PushUnsubscribeRequest(BaseModel):
    """`POST /api/push/unsubscribe` body. The browser only knows the URL of
    the sub it just dropped, so the body carries that one endpoint. Empty
    body is also valid — flips the global toggle off without removing rows."""

    endpoint: str | None = None


class PushPreferencesUpdate(BaseModel):
    """`POST /api/push/preferences`. All fields optional; only the keys
    actually present in the request are applied (mirrors v1's `if "x" in data`
    semantics)."""

    push_enabled: bool | None = None
    quiet_start: int | None = None  # 0-23
    quiet_end: int | None = None  # 0-23
    timezone: str | None = None


class PushTestRequest(BaseModel):
    title: str | None = "Test"
    body: str | None = "Hello from NEXTPLAY"


class VapidKeyResponse(BaseModel):
    """`GET /api/push/vapid-key`. v1 returns `{key, configured}` so the JS
    can detect the no-VAPID-configured local-dev case."""

    key: str
    configured: bool


__all__ = [
    "PushKeysSchema",
    "PushPreferencesUpdate",
    "PushSubscribeRequest",
    "PushTestRequest",
    "PushUnsubscribeRequest",
    "VapidKeyResponse",
]
