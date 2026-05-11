"""Document Campaigns JSON router — Phase 2.4.

Endpoints (auth: org session; role-gated):

- POST /org/api/document-campaigns/preview-recipients  any active member;
                                                        returns {count}
- POST /org/api/document-campaigns                      org_admin /
                                                        region_manager /
                                                        branch_manager;
                                                        creates campaign +
                                                        deliveries + queues
                                                        SMS+email dispatch
- GET  /org/api/document-campaigns                      any active member,
                                                        org-scoped
- GET  /org/api/document-campaigns/{id}                 scoped read

Cross-org → 404. Soft-delete deferred (Part B).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership, require_role
from src.core.database import get_db
from src.core.exceptions import NotFoundError, ValidationError
from src.models.user_organizations import UserOrganization
from src.repositories.document_campaigns_repo import DocumentCampaignsRepository
from src.repositories.document_templates_repo import DocumentTemplatesRepository
from src.schemas.document_campaigns import DocumentCampaignOut
from src.services.document_campaign_service import (
    VALID_FILTER_TYPES,
    count_recipients,
    create_campaign_with_deliveries,
)
from src.services.document_send_worker import dispatch_delivery

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/document-campaigns", tags=["org-document-campaigns"])


# ---------------------------------------------------------------------------
# POST /preview-recipients — used by the modal to show "will send to N parents"
# ---------------------------------------------------------------------------


@router.post("/preview-recipients", response_model=dict)
async def preview_recipients(
    body: dict,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    rf = body.get("recipient_filter") or {"type": "all"}
    if rf.get("type") not in VALID_FILTER_TYPES:
        raise ValidationError("Invalid recipient_filter.type.", code="invalid_filter")
    n = await count_recipients(
        db,
        organization_id=membership.organization_id,
        recipient_filter=rf,
        actor=membership,
    )
    return {"count": n}


# ---------------------------------------------------------------------------
# POST / — create + dispatch in one shot (MVP)
# ---------------------------------------------------------------------------


@router.post("", response_model=DocumentCampaignOut, status_code=status.HTTP_201_CREATED)
async def create_and_send(
    body: dict,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager", "branch_manager")
    ),
) -> DocumentCampaignOut:
    """Create a campaign + N deliveries + queue per-delivery dispatch.

    Body:
        template_id: int (required)
        title: str (required)
        recipient_filter: dict (required, see service)
        delivery_channels: list[str] (default ["sms", "email"])
        expires_in_days: int | None
    """
    template_id = body.get("template_id")
    title = (body.get("title") or "").strip()
    rf = body.get("recipient_filter") or {"type": "all"}
    channels = body.get("delivery_channels") or ["sms", "email"]
    expires_in_days = body.get("expires_in_days")

    if not template_id:
        raise ValidationError("template_id is required.", code="missing_template_id")
    if not title:
        raise ValidationError("title is required.", code="missing_title")
    if rf.get("type") not in VALID_FILTER_TYPES:
        raise ValidationError("Invalid recipient_filter.type.", code="invalid_filter")
    if not isinstance(channels, list) or not channels:
        raise ValidationError("delivery_channels must be a non-empty list.", code="invalid_channels")
    for c in channels:
        if c not in ("sms", "email"):
            raise ValidationError(f"Unknown channel: {c!r}.", code="invalid_channel")

    tpl_repo = DocumentTemplatesRepository(db)
    template = await tpl_repo.get_for_org(template_id, membership.organization_id)
    if template is None or not template.is_active:
        raise NotFoundError("Template not found")

    try:
        campaign, deliveries = await create_campaign_with_deliveries(
            db,
            template=template,
            title=title,
            recipient_filter=rf,
            delivery_channels=channels,
            actor=membership,
            actor_email=getattr(request.state, "user", None) and request.state.user.email,
            expires_in_days=expires_in_days,
            request=request,
        )
    except ValueError as e:
        raise ValidationError(str(e), code="no_recipients")

    # Queue per-delivery dispatch. BackgroundTasks fire AFTER the response,
    # so even 500+ deliveries return fast. Each task opens its own session.
    for d in deliveries:
        background.add_task(dispatch_delivery, d.id)

    await db.flush()
    await db.refresh(campaign)
    return DocumentCampaignOut.model_validate(campaign)


# ---------------------------------------------------------------------------
# GET / — list
# ---------------------------------------------------------------------------


@router.get("", response_model=dict)
async def list_campaigns(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    repo = DocumentCampaignsRepository(db)
    rows = await repo.list_for_org_ordered(membership.organization_id)
    return {
        "campaigns": [
            DocumentCampaignOut.model_validate(c).model_dump(mode="json") for c in rows
        ]
    }


# ---------------------------------------------------------------------------
# GET /{id}
# ---------------------------------------------------------------------------


@router.get("/{campaign_id}", response_model=DocumentCampaignOut)
async def get_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> DocumentCampaignOut:
    repo = DocumentCampaignsRepository(db)
    c = await repo.get_for_org(campaign_id, membership.organization_id)
    if c is None:
        raise NotFoundError("Campaign not found")
    return DocumentCampaignOut.model_validate(c)


__all__ = ["router"]
