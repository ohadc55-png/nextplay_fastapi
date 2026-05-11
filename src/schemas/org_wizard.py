"""Pydantic shapes for /admin/api/orgs/wizard/* (Phase 1.8 — Org Creation Wizard).

4-step shape: Basics -> Branding -> Structure -> Contact. The front-end posts
each step's payload on Next; the final commit takes the full WizardCommit
envelope and creates the organization + first membership + invites the CEO
in one transaction.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from src.schemas.org_users import OrgRole

# ============================================================================
# Step 1 — Basics
# ============================================================================
class WizardStep1Basics(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    legal_name: str | None = Field(default=None, max_length=200)
    tax_id: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    slug: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")


# ============================================================================
# Step 2 — Branding
# ============================================================================
class WizardStep2Branding(BaseModel):
    logo_url: str | None = Field(default=None, max_length=500)
    primary_color: str = Field(
        default="#2563EB",
        pattern=r"^#[0-9A-Fa-f]{6}$",
    )
    subdomain: str | None = Field(
        default=None,
        max_length=63,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    )


# ============================================================================
# Step 3 — Structure & Plan
# ============================================================================
WizardStructureType = Literal["flat", "regions", "regions_branches"]
WizardOrgStatus = Literal["trial", "active", "suspended"]


class WizardStep3Structure(BaseModel):
    structure_type: WizardStructureType = "flat"
    monthly_fee_cents: int = Field(default=500_000, ge=0)  # 5,000 ש"ח default
    setup_fee_cents: int = Field(default=1_000_000, ge=0)  # 10,000 ש"ח default
    trial_days: int = Field(default=60, ge=0, le=365)
    contract_start: date = Field(default_factory=date.today)
    status: WizardOrgStatus = "trial"


# ============================================================================
# Step 4 — Primary Contact (CEO)
# ============================================================================
class WizardStep4Contact(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=50)
    role: OrgRole = "org_admin"
    send_invite_immediately: bool = True


# ============================================================================
# Final commit
# ============================================================================
class WizardCommit(BaseModel):
    """Combined payload posted to POST /admin/api/orgs/wizard/commit."""

    step1: WizardStep1Basics
    step2: WizardStep2Branding
    step3: WizardStep3Structure
    step4: WizardStep4Contact


class WizardCommitResult(BaseModel):
    """Response after a successful commit."""

    org_id: int
    slug: str
    ceo_invite_email_sent: bool


class WizardPreflightRequest(BaseModel):
    """POST /admin/api/orgs/wizard/preflight — slug + subdomain availability check.
    No DB writes."""

    slug: str
    subdomain: str | None = None


class WizardPreflightResult(BaseModel):
    slug_available: bool
    subdomain_available: bool


__all__ = [
    "WizardCommit",
    "WizardCommitResult",
    "WizardOrgStatus",
    "WizardPreflightRequest",
    "WizardPreflightResult",
    "WizardStep1Basics",
    "WizardStep2Branding",
    "WizardStep3Structure",
    "WizardStep4Contact",
    "WizardStructureType",
]
