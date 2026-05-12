"""DocumentTemplates repository — Phase 2.2.

Thin wrapper over OrgScopedRepository. Adds two domain-specific helpers:
- `list_for_org_filtered`: lists templates with optional category narrowing
  and a default `is_active=True` filter so soft-deleted templates don't
  show up in the management UI.
- `set_fields`: writes the form_fields + signature_zones JSON in a single
  flush (the marking UI saves both together via PATCH /fields).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document_templates import DocumentTemplate
from src.repositories.org_scoped_repository import OrgScopedRepository


class DocumentTemplatesRepository(OrgScopedRepository[DocumentTemplate]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, DocumentTemplate)

    async def list_for_org_filtered(
        self,
        organization_id: int | None,
        *,
        category: str | None = None,
        include_inactive: bool = False,
    ) -> list[DocumentTemplate]:
        """List templates in an org. Defensive: returns [] for None org_id."""
        if organization_id is None:
            return []
        stmt = select(DocumentTemplate).where(
            DocumentTemplate.organization_id == organization_id
        )
        if category is not None:
            stmt = stmt.where(DocumentTemplate.category == category)
        if not include_inactive:
            stmt = stmt.where(DocumentTemplate.is_active.is_(True))
        # Phase 2.5b — completed templates sink to the bottom; within each
        # bucket newest-first by creation time.
        stmt = stmt.order_by(
            DocumentTemplate.is_completed.asc(),
            DocumentTemplate.created_at.desc(),
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["DocumentTemplatesRepository"]
