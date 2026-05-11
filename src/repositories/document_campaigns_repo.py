"""DocumentCampaigns repository — Phase 2.4."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.document_campaigns import DocumentCampaign
from src.repositories.org_scoped_repository import OrgScopedRepository


class DocumentCampaignsRepository(OrgScopedRepository[DocumentCampaign]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, DocumentCampaign)

    async def list_for_org_ordered(
        self, organization_id: int | None
    ) -> list[DocumentCampaign]:
        """Newest-first list for the campaigns page."""
        if organization_id is None:
            return []
        stmt = (
            select(DocumentCampaign)
            .options(selectinload(DocumentCampaign.template))
            .where(DocumentCampaign.organization_id == organization_id)
            .order_by(DocumentCampaign.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["DocumentCampaignsRepository"]
