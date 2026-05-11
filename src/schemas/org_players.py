"""Pydantic shapes for /org/api/players/* (Phase 1.6 — Players + Contacts).

Splitting `PlayerOut` (the basic roster row, returned on list/detail) from
`PlayerContactOut` (decrypted PII, returned only by /contact endpoints) keeps
sensitive data off the default serializer — every contact READ must go
through the dedicated endpoint that audit-logs.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from src.schemas.common import ORMModel


# ============================================================================
# Player (basic roster)
# ============================================================================
class PlayerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    team_id: int
    number: int | None = Field(default=None, ge=0, le=999)
    position: str | None = None
    height: str | None = None
    weight: str | None = None
    age: int | None = Field(default=None, ge=4, le=120)
    dominant_hand: str | None = None
    notes: str | None = None


class PlayerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    number: int | None = Field(default=None, ge=0, le=999)
    position: str | None = None
    height: str | None = None
    weight: str | None = None
    age: int | None = Field(default=None, ge=4, le=120)
    dominant_hand: str | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    notes: str | None = None
    active: bool | None = None


class PlayerOut(ORMModel):
    id: int
    organization_id: int | None = None
    team_id: int | None = None
    name: str
    number: int | None = None
    position: str | None = None
    height: str | None = None
    weight: str | None = None
    age: int | None = None
    dominant_hand: str | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    notes: str | None = None
    active: bool | None = None
    photo_url: str | None = None
    created_at: datetime | None = None


# ============================================================================
# PlayerContact (decrypted on the way out — Audit-logged on every read)
# ============================================================================
class PlayerContactUpsert(BaseModel):
    """PUT /org/api/players/{id}/contact — overwrite the contact row.
    Any field set to `None` is left unchanged; pass an empty string to clear
    a column. The encryption happens server-side via `EncryptedText`."""

    parent_name: str | None = None
    parent_email: EmailStr | None = None
    parent_phone: str | None = None
    national_id: str | None = None
    medical_notes: str | None = None
    address: str | None = None


class PlayerContactOut(ORMModel):
    """GET /org/api/players/{id}/contact — sensitive fields are decrypted
    by the EncryptedText TypeDecorator before this serializer ever sees
    them. Every read of this shape MUST trigger a `player.contact.read`
    audit log entry in the route handler."""

    player_id: int
    organization_id: int | None = None
    parent_name: str | None = None
    parent_email: str | None = None
    parent_phone: str | None = None  # decrypted from parent_phone_enc
    national_id: str | None = None  # decrypted from national_id_enc
    medical_notes: str | None = None  # decrypted from medical_notes_enc
    address: str | None = None  # decrypted from address_enc
    updated_at: datetime | None = None


__all__ = [
    "PlayerContactOut",
    "PlayerContactUpsert",
    "PlayerCreate",
    "PlayerOut",
    "PlayerUpdate",
]
