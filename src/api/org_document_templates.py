"""Document Templates JSON router — Phase 2.2.

CRUD for `document_templates` under /org/api/document-templates/*.

Auth & roles (org session via `get_current_org_membership`):

- GET    /org/api/document-templates                    any active member
- POST   /org/api/document-templates                    org_admin / region_manager /
                                                        branch_manager  (multipart upload)
- GET    /org/api/document-templates/{id}               any active member, scoped
- PATCH  /org/api/document-templates/{id}               org_admin / region_manager /
                                                        branch_manager  (metadata only)
- PATCH  /org/api/document-templates/{id}/fields        same  (form_fields + signature_zones)
- GET    /org/api/document-templates/{id}/preview       any active member, scoped (PNG stream)
- DELETE /org/api/document-templates/{id}               org_admin  (soft: is_active=False)

Cross-org access → 404 (never 403). Soft-delete preserves the S3 file —
old deliveries remain readable. No orphan cleanup yet (Part B concern).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership, require_role
from src.core.database import get_db
from src.core.exceptions import NotFoundError, ValidationError
from src.models.user_organizations import UserOrganization
from src.repositories.document_templates_repo import DocumentTemplatesRepository
from src.schemas.document_templates import (
    DocumentTemplateOut,
    DocumentTemplateUpdate,
    TemplateFieldsUpdate,
)
from src.services.document_template_service import (
    process_uploaded_file,
    render_template_preview,
)
from src.services.org_audit_service import log_org_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/document-templates", tags=["org-document-templates"])


_VALID_CATEGORIES = {
    "PARTICIPATION", "TOURNAMENT", "SIZING", "HEALTH", "PERMISSION", "OTHER"
}


# ---------------------------------------------------------------------------
# GET /org/api/document-templates — list
# ---------------------------------------------------------------------------


@router.get("", response_model=dict)
async def list_templates(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
    category: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
) -> dict:
    """List org's templates. Soft-deleted templates excluded unless
    `?include_inactive=true`."""
    repo = DocumentTemplatesRepository(db)
    rows = await repo.list_for_org_filtered(
        membership.organization_id,
        category=category,
        include_inactive=include_inactive,
    )
    return {
        "templates": [
            DocumentTemplateOut.model_validate(t).model_dump(mode="json") for t in rows
        ]
    }


# ---------------------------------------------------------------------------
# POST /org/api/document-templates — upload
# ---------------------------------------------------------------------------


@router.post("", response_model=DocumentTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    request: Request,
    file: UploadFile = File(..., description="PDF or DOCX file (max 10 MB)"),
    name: str = Form(..., min_length=1, max_length=200),
    description: str | None = Form(default=None),
    category: str = Form(default="OTHER"),
    requires_signature: bool = Form(default=True),
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager", "branch_manager")
    ),
) -> DocumentTemplateOut:
    """Multipart upload + create. Returns the new template; the field-
    marking UI then PATCHes /fields to set form_fields + signature_zones."""
    if category not in _VALID_CATEGORIES:
        raise ValidationError(
            f"Unknown category {category!r}.", code="invalid_category"
        )

    template = await process_uploaded_file(
        file,
        organization_id=membership.organization_id,
        name=name,
        description=description,
        category=category,
        requires_signature=requires_signature,
        created_by_user_id=request.state.user.id,
    )
    db.add(template)
    await db.flush()
    await db.refresh(template)

    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="document.template.create",
        target_type="document_template",
        target_id=template.id,
        request=request,
        extra={
            "name": template.name,
            "category": template.category,
            "file_type": template.uploaded_file_type,
            "size_bytes": template.uploaded_file_size,
        },
    )
    return DocumentTemplateOut.model_validate(template)


# ---------------------------------------------------------------------------
# GET /org/api/document-templates/{id}
# ---------------------------------------------------------------------------


@router.get("/{template_id}", response_model=DocumentTemplateOut)
async def get_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> DocumentTemplateOut:
    repo = DocumentTemplatesRepository(db)
    tpl = await repo.get_for_org(template_id, membership.organization_id)
    if tpl is None:
        raise NotFoundError("Template not found")
    return DocumentTemplateOut.model_validate(tpl)


# ---------------------------------------------------------------------------
# PATCH /org/api/document-templates/{id} — metadata update
# ---------------------------------------------------------------------------


@router.patch("/{template_id}", response_model=DocumentTemplateOut)
async def update_template(
    template_id: int,
    body: DocumentTemplateUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager", "branch_manager")
    ),
) -> DocumentTemplateOut:
    repo = DocumentTemplatesRepository(db)
    tpl = await repo.get_for_org(template_id, membership.organization_id)
    if tpl is None:
        raise NotFoundError("Template not found")

    changes: dict = {}
    if body.name is not None and body.name != tpl.name:
        changes["name"] = {"from": tpl.name, "to": body.name}
        tpl.name = body.name
    if body.description is not None and body.description != tpl.description:
        changes["description"] = "updated"
        tpl.description = body.description
    if body.category is not None and body.category != tpl.category:
        changes["category"] = {"from": tpl.category, "to": body.category}
        tpl.category = body.category
    if body.requires_signature is not None and body.requires_signature != tpl.requires_signature:
        changes["requires_signature"] = {
            "from": tpl.requires_signature, "to": body.requires_signature,
        }
        tpl.requires_signature = body.requires_signature
    if body.default_expiry_days is not None and body.default_expiry_days != tpl.default_expiry_days:
        changes["default_expiry_days"] = {
            "from": tpl.default_expiry_days, "to": body.default_expiry_days,
        }
        tpl.default_expiry_days = body.default_expiry_days
    if body.is_active is not None and body.is_active != tpl.is_active:
        changes["is_active"] = {"from": tpl.is_active, "to": body.is_active}
        tpl.is_active = body.is_active

    if changes:
        await db.flush()
        await db.refresh(tpl)
        await log_org_action(
            db,
            organization_id=membership.organization_id,
            actor_user_id=request.state.user.id,
            actor_email=request.state.user.email,
            action="document.template.update",
            target_type="document_template",
            target_id=tpl.id,
            request=request,
            extra=changes,
        )

    return DocumentTemplateOut.model_validate(tpl)


# ---------------------------------------------------------------------------
# PATCH /org/api/document-templates/{id}/fields — set form_fields + zones
# ---------------------------------------------------------------------------


@router.patch("/{template_id}/fields", response_model=DocumentTemplateOut)
async def set_template_fields(
    template_id: int,
    body: TemplateFieldsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "region_manager", "branch_manager")
    ),
) -> DocumentTemplateOut:
    """Replace form_fields + signature_zones together. Pydantic shapes
    enforce a closed field-type set, geometry sanity, and id uniqueness
    is the caller's responsibility (the editor maintains it client-side)."""
    repo = DocumentTemplatesRepository(db)
    tpl = await repo.get_for_org(template_id, membership.organization_id)
    if tpl is None:
        raise NotFoundError("Template not found")

    new_fields = [f.model_dump() for f in body.form_fields]
    new_zones = [z.model_dump() for z in body.signature_zones]

    # Reject duplicate field IDs early — they'd break form_response matching.
    all_ids = [f["id"] for f in new_fields] + [z["id"] for z in new_zones]
    if len(set(all_ids)) != len(all_ids):
        raise ValidationError(
            "Duplicate field/zone id in the payload.", code="duplicate_field_id"
        )

    tpl.form_fields = new_fields
    tpl.signature_zones = new_zones
    await db.flush()
    await db.refresh(tpl)

    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="document.template.fields_update",
        target_type="document_template",
        target_id=tpl.id,
        request=request,
        extra={
            "field_count": len(new_fields),
            "signature_zone_count": len(new_zones),
        },
    )
    return DocumentTemplateOut.model_validate(tpl)


# ---------------------------------------------------------------------------
# GET /org/api/document-templates/{id}/preview?page=N
# ---------------------------------------------------------------------------


@router.get("/{template_id}/preview")
async def preview_template(
    template_id: int,
    page: int = Query(default=1, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> Response:
    """Render template page N as a PNG. Used by the field-marking UI as
    the canvas background image."""
    repo = DocumentTemplatesRepository(db)
    tpl = await repo.get_for_org(template_id, membership.organization_id)
    if tpl is None:
        raise NotFoundError("Template not found")
    png = await render_template_preview(tpl, page=page)
    # Cache for 5 min — the file doesn't change after upload.
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


# ---------------------------------------------------------------------------
# DELETE /org/api/document-templates/{id} — soft-delete
# ---------------------------------------------------------------------------


@router.delete("/{template_id}", response_model=dict)
async def delete_template(
    template_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(require_role("org_admin")),
) -> dict:
    """Soft-delete via is_active=False. S3 file is preserved so historical
    deliveries remain readable. Returns 200 with {ok: True}; idempotent
    on already-soft-deleted templates."""
    repo = DocumentTemplatesRepository(db)
    tpl = await repo.get_for_org(template_id, membership.organization_id)
    if tpl is None:
        raise NotFoundError("Template not found")
    if not tpl.is_active:
        return {"ok": True}

    tpl.is_active = False
    await db.flush()

    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="document.template.delete",
        target_type="document_template",
        target_id=tpl.id,
        request=request,
        extra={"name": tpl.name},
    )
    return {"ok": True}


__all__ = ["router"]
