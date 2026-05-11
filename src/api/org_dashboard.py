"""Role-aware dashboard endpoints (Phase 1.7).

`GET /org/api/dashboard/summary` (Phase 0, kept lean) still returns the
org-wide tile counts every page header relies on. This module adds two
endpoints that drive the per-role dashboard cards:

- `GET /org/api/dashboard/role-stats`         scoped stat tiles for the active role
- `GET /org/api/dashboard/recent-activity`    audit-feed entries the role may see

Role matrix:
- org_admin:      org-wide counts + pending invites + last 20 audit rows
- region_manager: branches/teams/members IN their region
- branch_manager: teams/members/players IN their branch
- coach:          their teams + their players (links out to Coach App)

For recent-activity: org_admin sees every event in the org; everyone else
sees their own actions. The simpler scoping is intentional — region/branch
managers can ask for org-wide events via the audit-log admin tools later;
the dashboard ribbon focuses on "what did I do recently".
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership
from src.core.database import get_db
from src.models.branches import Branch
from src.models.org_audit import OrgAuditLog
from src.models.org_invites import OrgInvite
from src.models.players import Player
from src.models.regions import Region
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/dashboard", tags=["org-dashboard"])


# ---------------------------------------------------------------------------
# Helpers — count primitives
# ---------------------------------------------------------------------------


async def _count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar() or 0)


async def _stats_org_admin(db: AsyncSession, org_id: int) -> dict:
    member_count = await _count(db, select(func.count()).select_from(UserOrganization).where(
        UserOrganization.organization_id == org_id,
        UserOrganization.status == "active",
    ))
    branch_count = await _count(db, select(func.count()).select_from(Branch).where(
        Branch.organization_id == org_id,
    ))
    region_count = await _count(db, select(func.count()).select_from(Region).where(
        Region.organization_id == org_id,
    ))
    team_count = await _count(db, select(func.count()).select_from(TeamProfile).where(
        TeamProfile.organization_id == org_id,
    ))
    player_count = await _count(db, select(func.count()).select_from(Player).where(
        Player.organization_id == org_id, Player.active.is_(True),
    ))
    pending_invites = await _count(db, select(func.count()).select_from(OrgInvite).where(
        OrgInvite.organization_id == org_id, OrgInvite.status == "pending",
    ))
    return {
        "role": "org_admin",
        "tiles": [
            {"key": "members", "label": "חברי ארגון", "value": member_count, "href": "/org/users"},
            {"key": "regions", "label": "מחוזות", "value": region_count, "href": "/org/regions"},
            {"key": "branches", "label": "סניפים", "value": branch_count, "href": "/org/branches"},
            {"key": "teams", "label": "קבוצות", "value": team_count, "href": "/org/teams"},
            {"key": "players", "label": "שחקנים פעילים", "value": player_count, "href": "/org/players"},
            {"key": "pending_invites", "label": "הזמנות ממתינות", "value": pending_invites, "href": "/org/users"},
        ],
    }


async def _stats_region_manager(db: AsyncSession, org_id: int, region_id: int | None) -> dict:
    if region_id is None:
        return {"role": "region_manager", "tiles": [], "warning": "אין מחוז משויך"}

    branch_count = await _count(db, select(func.count()).select_from(Branch).where(
        Branch.organization_id == org_id, Branch.region_id == region_id,
    ))
    team_count = await _count(db, select(func.count()).select_from(TeamProfile).where(
        TeamProfile.organization_id == org_id,
        TeamProfile.branch_id.in_(
            select(Branch.id).where(Branch.region_id == region_id)
        ),
    ))
    member_count = await _count(db, select(func.count()).select_from(UserOrganization).where(
        UserOrganization.organization_id == org_id,
        UserOrganization.region_id == region_id,
        UserOrganization.status == "active",
    ))
    player_count = await _count(db, select(func.count()).select_from(Player).where(
        Player.organization_id == org_id,
        Player.active.is_(True),
        Player.team_id.in_(
            select(TeamProfile.id).where(
                TeamProfile.branch_id.in_(
                    select(Branch.id).where(Branch.region_id == region_id)
                )
            )
        ),
    ))
    return {
        "role": "region_manager",
        "region_id": region_id,
        "tiles": [
            {"key": "branches", "label": "סניפים במחוז", "value": branch_count, "href": "/org/branches"},
            {"key": "teams", "label": "קבוצות במחוז", "value": team_count, "href": "/org/teams"},
            {"key": "members", "label": "חברים במחוז", "value": member_count, "href": "/org/users"},
            {"key": "players", "label": "שחקנים במחוז", "value": player_count, "href": "/org/players"},
        ],
    }


async def _stats_branch_manager(db: AsyncSession, org_id: int, branch_id: int | None) -> dict:
    if branch_id is None:
        return {"role": "branch_manager", "tiles": [], "warning": "אין סניף משויך"}

    team_count = await _count(db, select(func.count()).select_from(TeamProfile).where(
        TeamProfile.organization_id == org_id, TeamProfile.branch_id == branch_id,
    ))
    member_count = await _count(db, select(func.count()).select_from(UserOrganization).where(
        UserOrganization.organization_id == org_id,
        UserOrganization.branch_id == branch_id,
        UserOrganization.status == "active",
    ))
    player_count = await _count(db, select(func.count()).select_from(Player).where(
        Player.organization_id == org_id,
        Player.active.is_(True),
        Player.team_id.in_(
            select(TeamProfile.id).where(TeamProfile.branch_id == branch_id)
        ),
    ))
    return {
        "role": "branch_manager",
        "branch_id": branch_id,
        "tiles": [
            {"key": "teams", "label": "קבוצות בסניף", "value": team_count, "href": "/org/teams"},
            {"key": "members", "label": "חברים בסניף", "value": member_count, "href": "/org/users"},
            {"key": "players", "label": "שחקנים בסניף", "value": player_count, "href": "/org/players"},
        ],
    }


async def _stats_coach(db: AsyncSession, org_id: int, user_id: int) -> dict:
    team_count = await _count(db, select(func.count()).select_from(TeamProfile).where(
        TeamProfile.organization_id == org_id, TeamProfile.user_id == user_id,
    ))
    player_count = await _count(db, select(func.count()).select_from(Player).where(
        Player.organization_id == org_id,
        Player.active.is_(True),
        Player.team_id.in_(
            select(TeamProfile.id).where(
                TeamProfile.organization_id == org_id,
                TeamProfile.user_id == user_id,
            )
        ),
    ))
    return {
        "role": "coach",
        "tiles": [
            {"key": "teams", "label": "הקבוצות שלי", "value": team_count, "href": "/org/teams"},
            {"key": "players", "label": "השחקנים שלי", "value": player_count, "href": "/org/players"},
            {"key": "coach_app", "label": "אפליקציית מאמן", "value": "→", "href": "/home"},
        ],
    }


# ---------------------------------------------------------------------------
# GET /org/api/dashboard/role-stats
# ---------------------------------------------------------------------------


@router.get("/role-stats", response_model=dict)
async def role_stats(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    org_id = membership.organization_id
    role = membership.role

    if role == "org_admin":
        return await _stats_org_admin(db, org_id)
    if role == "region_manager":
        return await _stats_region_manager(db, org_id, membership.region_id)
    if role == "branch_manager":
        return await _stats_branch_manager(db, org_id, membership.branch_id)
    if role == "coach":
        return await _stats_coach(db, org_id, membership.user_id)
    # viewer + any future role — return empty but valid envelope.
    return {"role": role, "tiles": []}


# ---------------------------------------------------------------------------
# GET /org/api/dashboard/recent-activity
# ---------------------------------------------------------------------------

# Action keys → Hebrew display label. Kept inline so the frontend doesn't
# need to ship its own translation table. Add new actions as features land.
_ACTION_LABELS_HE: dict[str, str] = {
    "region.create": "מחוז נוצר",
    "region.update": "מחוז עודכן",
    "region.delete": "מחוז נמחק",
    "branch.create": "סניף נוצר",
    "branch.update": "סניף עודכן",
    "branch.delete": "סניף נמחק",
    "user.invite": "הזמנה נשלחה",
    "user.invite.resend": "הזמנה נשלחה שוב",
    "user.invite.cancel": "הזמנה בוטלה",
    "user.invite.accept": "הזמנה התקבלה",
    "member.role_change": "תפקיד שונה",
    "member.scope_change": "סקופ שונה",
    "member.remove": "חבר הוסר",
    "team.create": "קבוצה נוצרה",
    "team.update": "קבוצה עודכנה",
    "team.delete": "קבוצה נמחקה",
    "team.coach_assign": "מאמן שויך",
    "team.coach_unassign": "מאמן הוסר",
    "player.create": "שחקן נוצר",
    "player.update": "שחקן עודכן",
    "player.delete": "שחקן הוסר",
    "player.contact.read": "פרטי קשר נצפו",
    "player.contact.write": "פרטי קשר עודכנו",
    "auth.org.login": "כניסה למערכת",
    "auth.org.switch_role": "החלפת תפקיד",
    "org.create": "ארגון נוצר",
    "org.update": "ארגון עודכן",
}


@router.get("/recent-activity", response_model=dict)
async def recent_activity(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
    limit: int = 20,
) -> dict:
    """Audit feed entries the active role may see.

    - org_admin: every event in the org.
    - everyone else: their own actions (actor_user_id == user.id).

    Cap limit at 100 to keep the response cheap."""
    capped_limit = max(1, min(limit, 100))

    stmt = (
        select(OrgAuditLog)
        .where(OrgAuditLog.organization_id == membership.organization_id)
        .order_by(OrgAuditLog.created_at.desc(), OrgAuditLog.id.desc())
        .limit(capped_limit)
    )
    if membership.role != "org_admin":
        stmt = stmt.where(OrgAuditLog.actor_user_id == membership.user_id)

    rows = list((await db.execute(stmt)).scalars().all())
    return {
        "events": [
            {
                "id": r.id,
                "actor_email": r.actor_email,
                "actor_user_id": r.actor_user_id,
                "action": r.action,
                "action_label": _ACTION_LABELS_HE.get(r.action, r.action),
                "target_type": r.target_type,
                "target_id": r.target_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


__all__ = ["router"]
