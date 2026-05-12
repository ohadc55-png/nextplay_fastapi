"""Document analytics aggregations — Phase 2.6a.

Read-only queries that summarise DocumentDelivery state per template /
region / branch / team. The dashboard binds against these.

Scoping: every query takes `organization_id`. Actor-scoping (region_manager
sees only their region, etc.) is enforced in the route layer because the
analytics surface is org-admin first; non-admin roles see filtered data
through the standard role helpers already used by the visibility view.

Performance: all queries are single-shot GROUP BY against indexed columns
(`organization_id` + `created_at` + `template_id` + `player_id`). For the
~10K-delivery scale we expect in Year-1 there's no need for pre-aggregated
tables.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.branches import Branch
from src.models.document_deliveries import DocumentDelivery
from src.models.document_templates import DocumentTemplate
from src.models.players import Player
from src.models.regions import Region
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization


def _period_start(days: int) -> datetime:
    days = max(1, min(days, 3650))  # clamp 1d..10y for sanity
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)


def _is_signed():
    return DocumentDelivery.document_status == "SIGNED"


def _scope_to_actor(stmt, actor: UserOrganization):
    """Same scoping shape as document_deliveries_view. Kept inline so we can
    join Player/Team/Branch only once per query when needed."""
    if actor.role == "region_manager":
        stmt = (
            stmt.join(Player, Player.id == DocumentDelivery.player_id)
            .join(TeamProfile, TeamProfile.id == Player.team_id)
            .join(Branch, Branch.id == TeamProfile.branch_id)
            .where(Branch.region_id == actor.region_id)
        )
    elif actor.role == "branch_manager":
        stmt = (
            stmt.join(Player, Player.id == DocumentDelivery.player_id)
            .join(TeamProfile, TeamProfile.id == Player.team_id)
            .where(TeamProfile.branch_id == actor.branch_id)
        )
    elif actor.role == "coach":
        stmt = (
            stmt.join(Player, Player.id == DocumentDelivery.player_id)
            .join(TeamProfile, TeamProfile.id == Player.team_id)
            .where(TeamProfile.user_id == actor.user_id)
        )
    return stmt


async def overview(
    db: AsyncSession,
    *,
    organization_id: int,
    actor: UserOrganization,
    days: int = 30,
) -> dict:
    """Top-level KPIs for the dashboard header."""
    since = _period_start(days)
    stmt = (
        select(
            func.count(DocumentDelivery.id).label("total"),
            func.sum(
                func.coalesce(
                    (DocumentDelivery.document_status == "SIGNED").cast(
                        type_=DocumentDelivery.id.type
                    ),
                    0,
                )
            ).label("signed"),
        )
        .where(DocumentDelivery.organization_id == organization_id)
        .where(DocumentDelivery.created_at >= since)
    )
    stmt = _scope_to_actor(stmt, actor)
    row = (await db.execute(stmt)).one()
    total = int(row.total or 0)
    signed = int(row.signed or 0)
    pct = round((signed / total) * 100, 1) if total else 0.0
    return {
        "period_days": days,
        "total_sent": total,
        "total_signed": signed,
        "signed_percentage": pct,
        "total_pending": total - signed,
    }


async def by_template(
    db: AsyncSession,
    *,
    organization_id: int,
    actor: UserOrganization,
    days: int = 30,
) -> list[dict]:
    """% signed per template, newest-template-first.

    Sort: by signed-percentage ASC (so the templates with the lowest
    completion rate float to the top — that's where the admin's attention
    should go).
    """
    since = _period_start(days)
    stmt = (
        select(
            DocumentTemplate.id.label("template_id"),
            DocumentTemplate.name.label("template_name"),
            DocumentTemplate.category.label("category"),
            func.count(DocumentDelivery.id).label("total"),
            func.sum(
                func.coalesce(
                    (DocumentDelivery.document_status == "SIGNED").cast(
                        type_=DocumentDelivery.id.type
                    ),
                    0,
                )
            ).label("signed"),
        )
        .join(DocumentTemplate, DocumentTemplate.id == DocumentDelivery.campaign_id)
        # Re-route the join via document_campaigns since deliveries link to campaigns,
        # not templates directly. We do the join in two steps in `_scope_to_actor`,
        # but the template lookup uses the campaign's template_id.
    )
    # Replace the join above with the right path: delivery → campaign → template.
    from src.models.document_campaigns import DocumentCampaign

    stmt = (
        select(
            DocumentTemplate.id.label("template_id"),
            DocumentTemplate.name.label("template_name"),
            DocumentTemplate.category.label("category"),
            func.count(DocumentDelivery.id).label("total"),
            func.sum(
                func.coalesce(
                    (DocumentDelivery.document_status == "SIGNED").cast(
                        type_=DocumentDelivery.id.type
                    ),
                    0,
                )
            ).label("signed"),
        )
        .join(DocumentCampaign, DocumentCampaign.id == DocumentDelivery.campaign_id)
        .join(DocumentTemplate, DocumentTemplate.id == DocumentCampaign.template_id)
        .where(DocumentDelivery.organization_id == organization_id)
        .where(DocumentDelivery.created_at >= since)
        .group_by(DocumentTemplate.id, DocumentTemplate.name, DocumentTemplate.category)
    )
    stmt = _scope_to_actor(stmt, actor)
    rows = (await db.execute(stmt)).all()
    out = []
    for r in rows:
        total = int(r.total or 0)
        signed = int(r.signed or 0)
        pct = round((signed / total) * 100, 1) if total else 0.0
        out.append({
            "template_id": r.template_id,
            "template_name": r.template_name,
            "category": r.category,
            "total": total,
            "signed": signed,
            "pending": total - signed,
            "signed_percentage": pct,
        })
    # Surface low-completion templates first; ties broken by larger volume.
    out.sort(key=lambda x: (x["signed_percentage"], -x["total"]))
    return out


async def _by_org_unit(
    db: AsyncSession,
    *,
    organization_id: int,
    actor: UserOrganization,
    days: int,
    unit: str,  # "region" | "branch" | "team"
) -> list[dict]:
    since = _period_start(days)

    # Build the join + group depending on the unit. All paths go through
    # Player → TeamProfile → (Branch → Region) so we share the joins.
    is_signed_int = func.coalesce(
        (DocumentDelivery.document_status == "SIGNED").cast(
            type_=DocumentDelivery.id.type
        ),
        0,
    )

    if unit == "team":
        stmt = (
            select(
                TeamProfile.id.label("unit_id"),
                TeamProfile.team_name.label("unit_name"),
                func.count(DocumentDelivery.id).label("total"),
                func.sum(is_signed_int).label("signed"),
            )
            .join(Player, Player.id == DocumentDelivery.player_id)
            .join(TeamProfile, TeamProfile.id == Player.team_id)
            .where(DocumentDelivery.organization_id == organization_id)
            .where(DocumentDelivery.created_at >= since)
            .group_by(TeamProfile.id, TeamProfile.team_name)
        )
    elif unit == "branch":
        stmt = (
            select(
                Branch.id.label("unit_id"),
                Branch.name.label("unit_name"),
                func.count(DocumentDelivery.id).label("total"),
                func.sum(is_signed_int).label("signed"),
            )
            .join(Player, Player.id == DocumentDelivery.player_id)
            .join(TeamProfile, TeamProfile.id == Player.team_id)
            .join(Branch, Branch.id == TeamProfile.branch_id)
            .where(DocumentDelivery.organization_id == organization_id)
            .where(DocumentDelivery.created_at >= since)
            .group_by(Branch.id, Branch.name)
        )
    else:  # region
        stmt = (
            select(
                Region.id.label("unit_id"),
                Region.name.label("unit_name"),
                func.count(DocumentDelivery.id).label("total"),
                func.sum(is_signed_int).label("signed"),
            )
            .join(Player, Player.id == DocumentDelivery.player_id)
            .join(TeamProfile, TeamProfile.id == Player.team_id)
            .join(Branch, Branch.id == TeamProfile.branch_id)
            .join(Region, Region.id == Branch.region_id)
            .where(DocumentDelivery.organization_id == organization_id)
            .where(DocumentDelivery.created_at >= since)
            .group_by(Region.id, Region.name)
        )

    # Layer in the actor scoping. The joins above already cover Player/Team/
    # Branch/Region in all three branches, so we just add WHERE clauses.
    if actor.role == "region_manager":
        stmt = stmt.where(Branch.region_id == actor.region_id)
    elif actor.role == "branch_manager":
        stmt = stmt.where(TeamProfile.branch_id == actor.branch_id)
    elif actor.role == "coach":
        stmt = stmt.where(TeamProfile.user_id == actor.user_id)

    rows = (await db.execute(stmt)).all()
    out = []
    for r in rows:
        total = int(r.total or 0)
        signed = int(r.signed or 0)
        pct = round((signed / total) * 100, 1) if total else 0.0
        out.append({
            "unit_id": r.unit_id,
            "unit_name": r.unit_name,
            "total": total,
            "signed": signed,
            "pending": total - signed,
            "signed_percentage": pct,
        })
    out.sort(key=lambda x: (x["signed_percentage"], -x["total"]))
    return out


async def by_region(db, *, organization_id, actor, days=30):
    return await _by_org_unit(
        db, organization_id=organization_id, actor=actor, days=days, unit="region",
    )


async def by_branch(db, *, organization_id, actor, days=30):
    return await _by_org_unit(
        db, organization_id=organization_id, actor=actor, days=days, unit="branch",
    )


async def by_team(db, *, organization_id, actor, days=30):
    return await _by_org_unit(
        db, organization_id=organization_id, actor=actor, days=days, unit="team",
    )


__all__ = [
    "by_branch",
    "by_region",
    "by_team",
    "by_template",
    "overview",
]
