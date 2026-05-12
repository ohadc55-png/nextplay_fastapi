"""DocumentDeliveries repository — Phase 2.3.

Used by the public signing router. The hot path is `get_by_token` —
called for every request to `/sign/{token}`, and it MUST not load related
campaign/template/player rows lazily (we'd MissingGreenlet inside a route
that already returned).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.document_campaigns import DocumentCampaign
from src.models.document_deliveries import DocumentDelivery
from src.repositories.org_scoped_repository import OrgScopedRepository


class DocumentDeliveriesRepository(OrgScopedRepository[DocumentDelivery]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, DocumentDelivery)

    async def get_by_token(self, token: str) -> DocumentDelivery | None:
        """Token lookup — the public /sign/{token} entry point.

        Eager-loads `campaign.template` and `player` so the route can
        render the signing page without further attribute fetches (the
        route's session may close before all template_response fields are
        materialized).
        """
        if not token:
            return None
        stmt = (
            select(DocumentDelivery)
            .options(
                selectinload(DocumentDelivery.campaign).selectinload(
                    DocumentCampaign.template
                ),
                selectinload(DocumentDelivery.player),
                selectinload(DocumentDelivery.organization),
            )
            .where(DocumentDelivery.unique_token == token)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["DocumentDeliveriesRepository"]
