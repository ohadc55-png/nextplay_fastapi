"""Sales inquiry schemas — `/api/contact-sales`."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field

from src.schemas.common import ORMModel


class SalesInquiryCreate(BaseModel):
    plan: str | None = None  # academy / enterprise
    full_name: str = Field(min_length=1)
    email: EmailStr
    organization: str | None = None
    country: str | None = ""
    num_coaches: str | None = ""
    num_teams: str | None = ""
    current_tools: str | None = ""
    notes: str | None = ""


class SalesInquiryResponse(ORMModel):
    id: int
    user_id: int | None = None
    plan: str | None = None
    full_name: str | None = None
    email: str | None = None
    organization: str | None = None
    country: str | None = None
    num_coaches: str | None = None
    num_teams: str | None = None
    current_tools: str | None = None
    notes: str | None = None
    status: str | None = None
    created_at: str | None = None


__all__ = ["SalesInquiryCreate", "SalesInquiryResponse"]
