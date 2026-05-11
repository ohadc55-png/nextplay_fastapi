"""Pydantic shapes for /org/api/document-templates/* (Phase 2.2).

Phase 2.1 lands the shapes; Phase 2.2 wires the endpoints that use them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.schemas.common import ORMModel

# Closed category set — matches the prompt; using Literal so we get a
# 422 on unknown values without an enum on the DB side.
TemplateCategory = Literal[
    "PARTICIPATION", "TOURNAMENT", "SIZING", "HEALTH", "PERMISSION", "OTHER"
]
TemplateFileType = Literal["PDF", "DOCX"]
FormFieldType = Literal["text", "select", "checkbox", "date", "number"]


class FormField(BaseModel):
    """One marked field on the template PDF. Coordinates are in PDF points
    (origin = top-left of the page when rendered)."""

    id: str = Field(min_length=1, max_length=40)
    type: FormFieldType
    label: str = Field(min_length=1, max_length=200)
    required: bool = False
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    page: int = Field(ge=1)
    options: list[str] | None = None  # for type='select'


class SignatureZone(BaseModel):
    """Rectangle where the parent's signature image will be embedded."""

    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=200)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    page: int = Field(ge=1)


class DocumentTemplateCreate(BaseModel):
    """Multipart-form payload metadata for POST /org/api/document-templates.
    The actual `file` field comes in via FastAPI UploadFile."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    category: TemplateCategory = "OTHER"
    requires_signature: bool = True


class DocumentTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    category: TemplateCategory | None = None
    requires_signature: bool | None = None
    default_expiry_days: int | None = Field(default=None, ge=1, le=365)
    is_active: bool | None = None


class TemplateFieldsUpdate(BaseModel):
    """PATCH /org/api/document-templates/{id}/fields body. Both lists
    fully replace the prior values (no merging)."""

    form_fields: list[FormField]
    signature_zones: list[SignatureZone]


class DocumentTemplateOut(ORMModel):
    id: int
    organization_id: int
    name: str
    description: str | None = None
    category: str
    uploaded_file_url: str  # S3 key — resolve to presigned URL on read if needed
    uploaded_file_type: str
    uploaded_file_size: int
    form_fields: list[dict] | None = None
    signature_zones: list[dict] | None = None
    requires_signature: bool
    default_expiry_days: int
    is_active: bool
    created_by_user_id: int | None = None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "DocumentTemplateCreate",
    "DocumentTemplateOut",
    "DocumentTemplateUpdate",
    "FormField",
    "SignatureZone",
    "TemplateCategory",
    "TemplateFieldsUpdate",
    "TemplateFileType",
]
