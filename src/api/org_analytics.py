"""Org Analytics router — Phase 2.6a.

Read-only endpoints powering /org/analytics:
- GET /org/api/analytics/overview?days=30
- GET /org/api/analytics/by-template?days=30
- GET /org/api/analytics/by-region?days=30
- GET /org/api/analytics/by-branch?days=30
- GET /org/api/analytics/by-team?days=30

All endpoints auto-scope to the actor's role via the analytics_service
helpers (region_manager → own region, branch_manager → own branch,
coach → own teams, org_admin → everything).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership
from src.core.database import get_db
from src.models.user_organizations import UserOrganization
from src.services import analytics_service

router = APIRouter(prefix="/org/api/analytics", tags=["org-analytics"])


def _days(days: int | None) -> int:
    # Clamp 1d..365d for the UI surface (deeper history available via SQL).
    if not days:
        return 30
    return max(1, min(int(days), 365))


@router.get("/overview", response_model=dict)
async def analytics_overview(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    return await analytics_service.overview(
        db,
        organization_id=membership.organization_id,
        actor=membership,
        days=_days(days),
    )


@router.get("/by-template", response_model=dict)
async def analytics_by_template(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    rows = await analytics_service.by_template(
        db,
        organization_id=membership.organization_id,
        actor=membership,
        days=_days(days),
    )
    return {"rows": rows, "period_days": _days(days)}


@router.get("/by-region", response_model=dict)
async def analytics_by_region(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    rows = await analytics_service.by_region(
        db,
        organization_id=membership.organization_id,
        actor=membership,
        days=_days(days),
    )
    return {"rows": rows, "period_days": _days(days)}


@router.get("/by-branch", response_model=dict)
async def analytics_by_branch(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    rows = await analytics_service.by_branch(
        db,
        organization_id=membership.organization_id,
        actor=membership,
        days=_days(days),
    )
    return {"rows": rows, "period_days": _days(days)}


@router.get("/by-team", response_model=dict)
async def analytics_by_team(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    rows = await analytics_service.by_team(
        db,
        organization_id=membership.organization_id,
        actor=membership,
        days=_days(days),
    )
    return {"rows": rows, "period_days": _days(days)}


__all__ = ["router"]
