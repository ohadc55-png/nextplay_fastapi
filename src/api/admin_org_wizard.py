"""Org Creation Wizard endpoints under /admin/api/orgs/wizard/* (Phase 1.8).

3 endpoints, all behind `get_current_admin` (System Admin session):

- POST /admin/api/orgs/wizard/preflight   slug + subdomain availability check
- POST /admin/api/orgs/wizard/upload-logo multipart logo upload (S3 or skipped
                                          if AWS not configured)
- POST /admin/api/orgs/wizard/commit      atomic create-org + invite CEO

The commit endpoint funnels through `org_wizard_service.commit_wizard` —
that's where the transactional logic lives. This module is just the HTTP
surface + multipart handling.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_admin
from src.core.database import get_db
from src.core.exceptions import ValidationError
from src.schemas.org_wizard import (
    WizardCommit,
    WizardCommitResult,
    WizardPreflightRequest,
    WizardPreflightResult,
)
from src.services import s3 as s3_service
from src.services.org_validators import validate_slug, validate_subdomain
from src.services.org_wizard_service import (
    check_slug_available,
    check_subdomain_available,
    commit_wizard,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api/orgs/wizard", tags=["admin-org-wizard"])

_MAX_LOGO_BYTES = 1 * 1024 * 1024  # 1 MB
_ALLOWED_LOGO_TYPES = {"image/png", "image/svg+xml", "image/jpeg", "image/webp"}


# ---------------------------------------------------------------------------
# POST /admin/api/orgs/wizard/preflight
# ---------------------------------------------------------------------------


@router.post("/preflight", response_model=WizardPreflightResult)
async def wizard_preflight(
    body: WizardPreflightRequest,
    _email: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> WizardPreflightResult:
    """Hint to the wizard UI: is the (slug, subdomain) pair still free? The
    final commit re-checks (this is a hint, not a reservation)."""
    slug = validate_slug(body.slug)
    slug_ok = await check_slug_available(db, slug)

    subdomain = validate_subdomain(body.subdomain)
    if subdomain is None:
        subdomain_ok = True  # empty subdomain is always "available"
    else:
        subdomain_ok = await check_subdomain_available(db, subdomain)

    return WizardPreflightResult(
        slug_available=slug_ok, subdomain_available=subdomain_ok,
    )


# ---------------------------------------------------------------------------
# POST /admin/api/orgs/wizard/upload-logo
# ---------------------------------------------------------------------------


@router.post("/upload-logo", response_model=dict)
async def wizard_upload_logo(
    file: Annotated[UploadFile, File(...)],
    slug: Annotated[str, Form(...)],
    _email: str = Depends(get_current_admin),
) -> dict:
    """Upload a logo to S3 under `orgs/{slug}/logo.{ext}` and return its URL.
    If S3 isn't configured locally, returns 503 — the wizard skips the field
    and proceeds with an empty logo_url (the org can edit it later)."""
    if file.content_type not in _ALLOWED_LOGO_TYPES:
        raise ValidationError(
            "Unsupported logo type. Use PNG, SVG, JPEG, or WebP.",
            code="invalid_logo_type",
        )

    contents = await file.read()
    if len(contents) > _MAX_LOGO_BYTES:
        raise ValidationError(
            f"Logo too large (max {_MAX_LOGO_BYTES // 1024} KB).",
            code="logo_too_large",
        )

    if not s3_service.is_configured():
        return {
            "logo_url": None,
            "skipped": True,
            "reason": "S3 not configured in this environment.",
        }

    safe_slug = validate_slug(slug)
    ext_map = {
        "image/png": "png", "image/svg+xml": "svg",
        "image/jpeg": "jpg", "image/webp": "webp",
    }
    ext = ext_map.get(file.content_type, "bin")
    key = f"orgs/{safe_slug}/logo-{uuid.uuid4().hex[:8]}.{ext}"
    await s3_service.put_bytes(
        key=key, data=contents, content_type=file.content_type,
    )
    logo_url = s3_service.get_video_url(key)
    return {"logo_url": logo_url, "key": key, "skipped": False}


# ---------------------------------------------------------------------------
# POST /admin/api/orgs/wizard/commit
# ---------------------------------------------------------------------------


@router.post("/commit", response_model=WizardCommitResult, status_code=status.HTTP_201_CREATED)
async def wizard_commit(
    body: WizardCommit,
    request: Request,
    background: BackgroundTasks,
    email: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> WizardCommitResult:
    """Final commit — create the org + first membership + audit + (optional)
    CEO invite. All in one transaction. Returns IDs for the redirect."""
    outcome = await commit_wizard(
        db,
        data=body,
        actor_email=email,
        request=request,
        background=background,
    )
    return WizardCommitResult(
        org_id=outcome.org_id,
        slug=outcome.slug,
        ceo_invite_email_sent=outcome.ceo_invite_sent,
    )


__all__ = ["router"]
