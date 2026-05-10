"""OrgScopedRepository — sibling of TeamScopedRepository for org-scoped tables.

Encodes the at-least-one-of pattern from `TeamScopedRepository` but along the
org axis: any query refuses to run unless an `organization_id` is provided.
This is the second layer of the three-layer tenancy defense (middleware →
repository → Postgres RLS).

Apply to any model that has an `organization_id` column. Does NOT replace
`TeamScopedRepository` — most existing tables stay scoped by (user_id, team_id).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.strategy_options import _AbstractLoad

from src.repositories.base_repository import BaseRepository, T


class OrgScopedRepository(BaseRepository[T]):
    """Repository for tables with `organization_id`.

    Defensive: returns ``[]`` from list and ``None`` from get when no org_id
    is provided. This prevents accidentally-unscoped queries from leaking
    cross-tenant data when a service forgets to thread the active org through.
    """

    async def list_for_org(
        self,
        organization_id: int | None,
        *,
        loaders: Sequence[_AbstractLoad] = (),
    ) -> list[T]:
        if organization_id is None:
            return []
        stmt = select(self.model).where(
            self.model.organization_id == organization_id  # type: ignore[attr-defined]
        )
        for loader in loaders:
            stmt = stmt.options(loader)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_for_org(
        self,
        id: Any,  # noqa: A002
        organization_id: int | None,
        *,
        loaders: Sequence[_AbstractLoad] = (),
    ) -> T | None:
        """PK lookup that ALSO enforces the org filter — returns None if the
        row exists but belongs to a different org. The route layer translates
        this to 404 (the `404-not-403` rule), so cross-org access never
        confirms a resource exists."""
        if organization_id is None:
            return None
        stmt = select(self.model).where(
            self.model.id == id,  # type: ignore[attr-defined]
            self.model.organization_id == organization_id,  # type: ignore[attr-defined]
        )
        for loader in loaders:
            stmt = stmt.options(loader)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


__all__ = ["OrgScopedRepository"]
