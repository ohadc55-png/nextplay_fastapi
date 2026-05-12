"""Messages JSON router — Phase 2.5.

Endpoints (auth: org session; role-gated):

- POST /org/api/messages/preview-recipients  any active member; {count}
- POST /org/api/messages                     org_admin / region_manager /
                                             branch_manager;
                                             body.save_as_draft=true keeps
                                             status=DRAFT, otherwise sends
- POST /org/api/messages/{id}/send           promote a DRAFT to SENDING
- PATCH /org/api/messages/{id}               edit a DRAFT
- DELETE /org/api/messages/{id}              delete a DRAFT (no soft-delete)
- GET  /org/api/messages?status=DRAFT|SENT   list, org-scoped
- GET  /org/api/messages/{id}                detail
- GET  /org/api/messages/{id}/deliveries     per-recipient rows

Cross-org → 404.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership, require_role
from src.core.database import get_db
from src.core.exceptions import NotFoundError, ValidationError
from src.models.user_organizations import UserOrganization
from src.repositories.messages_repo import (
    MessageDeliveriesRepository,
    MessagesRepository,
)
from src.schemas.messages import MessageDeliveryOut, MessageOut
from src.services.message_send_worker import dispatch_message_delivery
from src.services.message_service import (
    create_draft,
    create_message_with_deliveries,
    promote_draft_to_send,
    update_draft,
)
from src.services.org_audit_service import log_org_action
from src.services.recipient_resolver import (
    VALID_FILTER_TYPES,
    count_recipients,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/messages", tags=["org-messages"])


def _validate_channels(channels) -> list[str]:
    if not isinstance(channels, list) or not channels:
        raise ValidationError(
            "delivery_channels must be a non-empty list.", code="invalid_channels"
        )
    for c in channels:
        if c not in ("sms", "email"):
            raise ValidationError(f"Unknown channel: {c!r}.", code="invalid_channel")
    return list(channels)


def _actor_email(request: Request) -> str | None:
    user = getattr(request.state, "user", None)
    return getattr(user, "email", None) if user else None


# ---------------------------------------------------------------------------
# POST /preview-recipients — modal count
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
# POST / — create (draft or send)
# ---------------------------------------------------------------------------


@router.post("", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
async def create_message(
    body: dict,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager", "branch_manager")
    ),
) -> MessageOut:
    subject = (body.get("subject") or "").strip()
    msg_body = body.get("body") or ""
    rf = body.get("recipient_filter") or {"type": "all"}
    channels = body.get("delivery_channels") or ["sms", "email"]
    save_as_draft = bool(body.get("save_as_draft"))
    scheduled_at_raw = body.get("scheduled_at")

    if not subject:
        raise ValidationError("subject is required.", code="missing_subject")
    if not msg_body.strip():
        raise ValidationError("body is required.", code="missing_body")
    if rf.get("type") not in VALID_FILTER_TYPES:
        raise ValidationError("Invalid recipient_filter.type.", code="invalid_filter")
    channels = _validate_channels(channels)

    # Phase 2.6c — scheduled send. Parse the ISO string into a future datetime.
    scheduled_at = None
    if scheduled_at_raw:
        from datetime import UTC, datetime
        try:
            scheduled_at = datetime.fromisoformat(str(scheduled_at_raw).replace("Z", "+00:00"))
            if scheduled_at.tzinfo is not None:
                scheduled_at = scheduled_at.astimezone(UTC).replace(tzinfo=None)
            now = datetime.now(UTC).replace(tzinfo=None)
            if scheduled_at <= now:
                raise ValidationError(
                    "scheduled_at must be in the future.", code="schedule_in_past",
                )
        except ValueError:
            raise ValidationError(
                "Invalid scheduled_at format. Use ISO 8601.", code="invalid_scheduled_at",
            )

    if scheduled_at is not None:
        # Save as SCHEDULED — no deliveries created until cron promotes it.
        msg = await create_draft(
            db,
            organization_id=membership.organization_id,
            subject=subject,
            body=msg_body,
            recipient_filter=rf,
            delivery_channels=channels,
            actor=membership,
        )
        msg.status = "SCHEDULED"
        msg.scheduled_at = scheduled_at
        await db.flush()
        await log_org_action(
            db,
            organization_id=membership.organization_id,
            actor_user_id=membership.user_id,
            actor_email=_actor_email(request),
            action="message.schedule",
            target_type="message",
            target_id=msg.id,
            request=request,
            extra={"channels": channels, "scheduled_at": scheduled_at.isoformat()},
        )
        await db.refresh(msg)
        return MessageOut.model_validate(msg)

    if save_as_draft:
        msg = await create_draft(
            db,
            organization_id=membership.organization_id,
            subject=subject,
            body=msg_body,
            recipient_filter=rf,
            delivery_channels=channels,
            actor=membership,
        )
        await log_org_action(
            db,
            organization_id=membership.organization_id,
            actor_user_id=membership.user_id,
            actor_email=_actor_email(request),
            action="message.draft.create",
            target_type="message",
            target_id=msg.id,
            request=request,
            extra={"channels": channels},
        )
        await db.flush()
        await db.refresh(msg)
        return MessageOut.model_validate(msg)

    try:
        msg, deliveries = await create_message_with_deliveries(
            db,
            organization_id=membership.organization_id,
            subject=subject,
            body=msg_body,
            recipient_filter=rf,
            delivery_channels=channels,
            actor=membership,
            actor_email=_actor_email(request),
            request=request,
        )
    except ValueError as e:
        raise ValidationError(str(e), code="no_recipients")

    for d in deliveries:
        background.add_task(dispatch_message_delivery, d.id)

    await db.flush()
    await db.refresh(msg)
    return MessageOut.model_validate(msg)


# ---------------------------------------------------------------------------
# POST /{id}/send — promote DRAFT to SENDING
# ---------------------------------------------------------------------------


@router.post("/{message_id}/send", response_model=MessageOut)
async def send_draft(
    message_id: int,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager", "branch_manager")
    ),
) -> MessageOut:
    repo = MessagesRepository(db)
    msg = await repo.get_for_org(message_id, membership.organization_id)
    if msg is None:
        raise NotFoundError("Message not found")
    if msg.status != "DRAFT":
        raise ValidationError("Only DRAFT messages can be sent.", code="not_draft")
    try:
        msg, deliveries = await promote_draft_to_send(
            db,
            message=msg,
            actor=membership,
            actor_email=_actor_email(request),
            request=request,
        )
    except ValueError as e:
        raise ValidationError(str(e), code="no_recipients")
    for d in deliveries:
        background.add_task(dispatch_message_delivery, d.id)
    await db.flush()
    await db.refresh(msg)
    return MessageOut.model_validate(msg)


# ---------------------------------------------------------------------------
# PATCH /{id} — edit DRAFT
# ---------------------------------------------------------------------------


@router.patch("/{message_id}", response_model=MessageOut)
async def patch_draft(
    message_id: int,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager", "branch_manager")
    ),
) -> MessageOut:
    repo = MessagesRepository(db)
    msg = await repo.get_for_org(message_id, membership.organization_id)
    if msg is None:
        raise NotFoundError("Message not found")
    if msg.status != "DRAFT":
        raise ValidationError("Only DRAFT messages can be edited.", code="not_draft")

    channels = body.get("delivery_channels")
    if channels is not None:
        channels = _validate_channels(channels)
    rf = body.get("recipient_filter")
    if rf is not None and rf.get("type") not in VALID_FILTER_TYPES:
        raise ValidationError("Invalid recipient_filter.type.", code="invalid_filter")

    try:
        msg = await update_draft(
            db,
            message=msg,
            subject=body.get("subject"),
            body=body.get("body"),
            recipient_filter=rf,
            delivery_channels=channels,
        )
    except ValueError as e:
        raise ValidationError(str(e), code="not_draft")

    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=membership.user_id,
        actor_email=_actor_email(request),
        action="message.draft.update",
        target_type="message",
        target_id=msg.id,
        request=request,
    )
    await db.refresh(msg)
    return MessageOut.model_validate(msg)


# ---------------------------------------------------------------------------
# DELETE /{id} — drop a DRAFT (no soft-delete)
# ---------------------------------------------------------------------------


@router.delete("/{message_id}", response_model=dict)
async def delete_draft(
    message_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager", "branch_manager")
    ),
) -> dict:
    repo = MessagesRepository(db)
    msg = await repo.get_for_org(message_id, membership.organization_id)
    if msg is None:
        raise NotFoundError("Message not found")
    if msg.status != "DRAFT":
        raise ValidationError(
            "Only DRAFT messages can be deleted.", code="not_draft"
        )
    await db.delete(msg)
    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=membership.user_id,
        actor_email=_actor_email(request),
        action="message.draft.delete",
        target_type="message",
        target_id=message_id,
        request=request,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# GET / — list
# ---------------------------------------------------------------------------


@router.get("", response_model=dict)
async def list_messages(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    if status_filter and status_filter not in (
        "DRAFT", "SCHEDULED", "SENDING", "SENT", "CANCELLED"
    ):
        raise ValidationError("Unknown status filter.", code="invalid_status")
    repo = MessagesRepository(db)
    rows = await repo.list_for_org_ordered(
        membership.organization_id, status_filter=status_filter
    )
    return {
        "messages": [
            MessageOut.model_validate(m).model_dump(mode="json") for m in rows
        ]
    }


# ---------------------------------------------------------------------------
# GET /{id}
# ---------------------------------------------------------------------------


@router.get("/{message_id}", response_model=MessageOut)
async def get_message(
    message_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> MessageOut:
    repo = MessagesRepository(db)
    msg = await repo.get_for_org(message_id, membership.organization_id)
    if msg is None:
        raise NotFoundError("Message not found")
    return MessageOut.model_validate(msg)


# ---------------------------------------------------------------------------
# GET /{id}/deliveries
# ---------------------------------------------------------------------------


@router.get("/{message_id}/deliveries", response_model=dict)
async def list_deliveries(
    message_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    msg_repo = MessagesRepository(db)
    msg = await msg_repo.get_for_org(message_id, membership.organization_id)
    if msg is None:
        raise NotFoundError("Message not found")
    deliveries_repo = MessageDeliveriesRepository(db)
    rows = await deliveries_repo.list_for_message(
        message_id, membership.organization_id
    )
    return {
        "deliveries": [
            MessageDeliveryOut.model_validate(d).model_dump(mode="json") for d in rows
        ]
    }


__all__ = ["router"]
