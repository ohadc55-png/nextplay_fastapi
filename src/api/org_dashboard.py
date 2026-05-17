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
from src.models.org_audit import OrgAuditLog
from src.models.org_invites import OrgInvite
from src.models.players import Player
from src.models.programs import Program
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
    region_count = await _count(db, select(func.count()).select_from(Region).where(
        Region.organization_id == org_id,
    ))
    program_count = await _count(db, select(func.count()).select_from(Program).where(
        Program.organization_id == org_id,
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
            {"key": "programs", "label": "תוכניות", "value": program_count, "href": "/org/programs"},
            {"key": "regions", "label": "מחוזות", "value": region_count, "href": "/org/regions"},
            {"key": "teams", "label": "קבוצות", "value": team_count, "href": "/org/teams"},
            {"key": "players", "label": "שחקנים פעילים", "value": player_count, "href": "/org/players"},
            {"key": "pending_invites", "label": "הזמנות ממתינות", "value": pending_invites, "href": "/org/users"},
        ],
    }


async def _stats_program_manager(
    db: AsyncSession, org_id: int, program_id: int | None,
) -> dict:
    """4-tile program-scoped overview: regions / teams / members / players.

    Phase 12 — after decoupling regions from programs, "regions in this
    program" means "distinct regions where this program has teams" rather
    than "regions whose program_id = X" (the latter is now always NULL).
    Teams/players are scoped via TeamProfile.program_id directly.
    """
    if program_id is None:
        return {"role": "program_manager", "tiles": [], "warning": "אין תוכנית משויכת"}

    program = await db.get(Program, program_id)
    program_name = program.name if program else None

    # Subquery: all team ids belonging to this program. Used by player
    # count + by the region-distinct count. Cheaper than a 3-way join.
    program_teams_subq = select(TeamProfile.id).where(
        TeamProfile.organization_id == org_id,
        TeamProfile.program_id == program_id,
    )

    region_count = await _count(
        db,
        select(func.count(func.distinct(TeamProfile.region_id))).where(
            TeamProfile.organization_id == org_id,
            TeamProfile.program_id == program_id,
            TeamProfile.region_id.is_not(None),
        ),
    )
    team_count = await _count(db, select(func.count()).select_from(TeamProfile).where(
        TeamProfile.organization_id == org_id,
        TeamProfile.program_id == program_id,
    ))
    # Members in this program: pinned via UserOrganization.program_id. We
    # no longer fold in region_managers — with shared regions, a single
    # region can host teams from many programs, so a region_manager isn't
    # a "member of program X" by virtue of geography alone.
    member_count = await _count(db, select(func.count()).select_from(UserOrganization).where(
        UserOrganization.organization_id == org_id,
        UserOrganization.status == "active",
        UserOrganization.program_id == program_id,
    ))
    player_count = await _count(db, select(func.count()).select_from(Player).where(
        Player.organization_id == org_id,
        Player.active.is_(True),
        Player.team_id.in_(program_teams_subq),
    ))

    return {
        "role": "program_manager",
        "program_id": program_id,
        "program_name": program_name,
        "tiles": [
            {"key": "regions", "label": "מחוזות בתוכנית", "value": region_count, "href": "/org/regions"},
            {"key": "teams", "label": "קבוצות בתוכנית", "value": team_count, "href": "/org/teams"},
            {"key": "members", "label": "חברים בתוכנית", "value": member_count, "href": "/org/users"},
            {"key": "players", "label": "שחקנים בתוכנית", "value": player_count, "href": "/org/players"},
        ],
    }


async def _stats_region_manager(
    db: AsyncSession,
    org_id: int,
    region_id: int | None,
    program_id: int | None = None,
) -> dict:
    """4-tile region-scoped overview.

    Phase 12 — when the RM membership carries a `program_id`, the dashboard
    narrows to that program × region intersection (not just the region).
    With shared regions a single region can host teams from many programs;
    pinning an RM to a program lets the org carve out an RM whose scope is
    "Haifa, Sal-Tek only" rather than "Haifa, all programs".

    Without a pinned program (`program_id=None`), counts span every team in
    the region. Player count goes via teams_in_scope; member count is
    sliced from UserOrganization by region (+ program when pinned).
    """
    if region_id is None:
        return {"role": "region_manager", "tiles": [], "warning": "אין מחוז משויך"}

    # Resolve program context for the header subtitle.
    program_name: str | None = None
    if program_id is not None:
        prog = await db.get(Program, program_id)
        program_name = prog.name if prog else None

    # Branch path is Phase-0 legacy. For Sha'ar Shivyon teams are scoped
    # via team.region_id directly. The branch path is preserved for legacy
    # orgs that still use it.
    teams_in_scope_q = (
        select(TeamProfile.id)
        .where(
            TeamProfile.organization_id == org_id,
            TeamProfile.region_id == region_id,
        )
    )
    if program_id is not None:
        teams_in_scope_q = teams_in_scope_q.where(TeamProfile.program_id == program_id)

    team_count_q = (
        select(func.count())
        .select_from(TeamProfile)
        .where(
            TeamProfile.organization_id == org_id,
            TeamProfile.region_id == region_id,
        )
    )
    if program_id is not None:
        team_count_q = team_count_q.where(TeamProfile.program_id == program_id)

    member_count_q = (
        select(func.count())
        .select_from(UserOrganization)
        .where(
            UserOrganization.organization_id == org_id,
            UserOrganization.region_id == region_id,
            UserOrganization.status == "active",
        )
    )
    if program_id is not None:
        member_count_q = member_count_q.where(UserOrganization.program_id == program_id)

    team_count = await _count(db, team_count_q)
    member_count = await _count(db, member_count_q)
    player_count = await _count(db, select(func.count()).select_from(Player).where(
        Player.organization_id == org_id,
        Player.active.is_(True),
        Player.team_id.in_(teams_in_scope_q),
    ))
    # An RM is always tied to a (region × program) slice in Phase 12. Tiles
    # read "in your program" since that's the canonical scope for the role.
    return {
        "role": "region_manager",
        "region_id": region_id,
        "program_id": program_id,
        "program_name": program_name,
        "tiles": [
            {"key": "teams", "label": "קבוצות בתוכנית", "value": team_count, "href": "/org/teams"},
            {"key": "members", "label": "חברים בתוכנית", "value": member_count, "href": "/org/users"},
            {"key": "players", "label": "שחקנים בתוכנית", "value": player_count, "href": "/org/players"},
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
    if role == "program_manager":
        return await _stats_program_manager(
            db, org_id, getattr(membership, "program_id", None),
        )
    if role == "region_manager":
        return await _stats_region_manager(
            db, org_id, membership.region_id,
            getattr(membership, "program_id", None),
        )
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


# ---------------------------------------------------------------------------
# GET /org/api/dashboard/breakdown
# ---------------------------------------------------------------------------
#
# Drives the two visual charts on the redesigned dashboard:
#   - bars  → horizontal-bar chart (Region/Team count by next-level-down)
#   - donut → pie/donut of share-by-program (org_admin only; PM/RM degrade)
#
# Why ONE endpoint with both: the dashboard renders both charts side-by-side.
# Two endpoints would double the round-trip; combining them keeps the page
# render path single-shot. Response is role-aware — coach/viewer get empty
# rows + None donut, so the JS gracefully hides both panels.


async def _breakdown_org_admin(db: AsyncSession, org_id: int) -> dict:
    bars_q = await db.execute(
        select(Region.name, func.count(TeamProfile.id))
        .select_from(Region)
        .outerjoin(
            TeamProfile,
            (TeamProfile.region_id == Region.id)
            & (TeamProfile.organization_id == org_id),
        )
        .where(Region.organization_id == org_id)
        .group_by(Region.id, Region.name)
        .order_by(func.count(TeamProfile.id).desc())
        .limit(8)
    )
    bars_rows = [
        {"name": (n or "—"), "value": int(c or 0)} for (n, c) in bars_q.all()
    ]

    # Phase 12 — donut uses TeamProfile.program_id directly so a region
    # ("מחוז מרכז") can show teams from every program at once, rather than
    # being locked 1:1 to a single program via Region.program_id.
    donut_q = await db.execute(
        select(Program.name, func.count(TeamProfile.id))
        .select_from(Program)
        .outerjoin(
            TeamProfile,
            (TeamProfile.program_id == Program.id)
            & (TeamProfile.organization_id == org_id),
        )
        .where(Program.organization_id == org_id)
        .group_by(Program.id, Program.name)
        .order_by(func.count(TeamProfile.id).desc())
        .limit(6)
    )
    donut_items = [
        ((n or "—"), int(c or 0)) for (n, c) in donut_q.all() if (c or 0) > 0
    ]
    return {
        "bars": {"label": "קבוצות לפי מחוז", "rows": bars_rows},
        "donut": _as_donut(donut_items, label="פעילות לפי תוכנית", unit="קבוצות"),
    }


async def _breakdown_program_manager(
    db: AsyncSession, org_id: int, program_id: int | None
) -> dict:
    if program_id is None:
        return {"bars": {"label": "קבוצות לפי מחוז", "rows": []}, "donut": None}
    # Phase 12 — regions are now shared across programs, so we filter the
    # JOINED TeamProfile by program_id rather than restricting the Region
    # set. Every region the org has shows up; values reflect just this PM's
    # program.
    bars_q = await db.execute(
        select(Region.name, func.count(TeamProfile.id))
        .select_from(Region)
        .outerjoin(
            TeamProfile,
            (TeamProfile.region_id == Region.id)
            & (TeamProfile.organization_id == org_id)
            & (TeamProfile.program_id == program_id),
        )
        .where(Region.organization_id == org_id)
        .group_by(Region.id, Region.name)
        .order_by(func.count(TeamProfile.id).desc())
        .limit(8)
    )
    rows = [{"name": (n or "—"), "value": int(c or 0)} for (n, c) in bars_q.all()]
    # PM only sees their single program. Reuse the same data as a donut so
    # the panel doesn't sit empty — it now reads "share of program by region".
    donut_items = [(r["name"], r["value"]) for r in rows if r["value"] > 0]
    return {
        "bars": {"label": "קבוצות לפי מחוז (בתוכנית שלך)", "rows": rows},
        "donut": _as_donut(donut_items, label="חלוקת קבוצות במחוזות", unit="קבוצות"),
    }


async def _breakdown_region_manager(
    db: AsyncSession,
    org_id: int,
    region_id: int | None,
    program_id: int | None = None,
) -> dict:
    """Bars chart for an RM. When the RM is pinned to a single program (a PM
    invited them), `program_id` further narrows the chart to that program ×
    region — otherwise we show every team in the region across all programs.
    The donut stays None either way (a single region can hold many programs,
    but for an RM we keep the page lean — bars are enough)."""
    if region_id is None:
        return {"bars": {"label": "קבוצות באזור", "rows": []}, "donut": None}
    bars_q = (
        select(TeamProfile.team_name, func.count(Player.id))
        .select_from(TeamProfile)
        .outerjoin(
            Player,
            (Player.team_id == TeamProfile.id) & (Player.active.is_(True)),
        )
        .where(
            TeamProfile.organization_id == org_id,
            TeamProfile.region_id == region_id,
        )
        .group_by(TeamProfile.id, TeamProfile.team_name)
        .order_by(func.count(Player.id).desc())
        .limit(8)
    )
    if program_id is not None:
        bars_q = bars_q.where(TeamProfile.program_id == program_id)
    rows = [
        {"name": (n or "—"), "value": int(c or 0)}
        for (n, c) in (await db.execute(bars_q)).all()
    ]
    return {
        "bars": {"label": "שחקנים פעילים לפי קבוצה", "rows": rows},
        "donut": None,
    }


def _as_donut(
    items: list[tuple[str, int]], *, label: str, unit: str
) -> dict | None:
    """Build the donut response from raw (name, count) tuples.

    Returns None when the data isn't meaningful (no rows or one row), so
    the frontend hides the panel instead of rendering a degenerate chart."""
    items = [(n, c) for (n, c) in items if c > 0]
    if len(items) < 2:
        return None
    total = sum(c for (_, c) in items)
    return {
        "label": label,
        "rows": [
            {"name": n, "value": c, "percent": round(c * 100 / total)}
            for (n, c) in items
        ],
        "total": total,
        "total_label": unit,
    }


@router.get("/breakdown", response_model=dict)
async def chart_breakdown(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    """Role-aware chart data for the dashboard (bars + donut).

    Response shape:
        {
          "bars":  {"label": "...", "rows": [{"name", "value"}, ...]},
          "donut": {"label": "...", "rows": [{name, value, percent}], "total", "total_label"} | None,
        }

    Callers should hide the donut panel when `donut is None` (PM with a
    one-region scope, RM, coach, viewer)."""
    org_id = membership.organization_id
    role = membership.role
    if role == "org_admin":
        return await _breakdown_org_admin(db, org_id)
    if role == "program_manager":
        return await _breakdown_program_manager(
            db, org_id, getattr(membership, "program_id", None)
        )
    if role == "region_manager":
        return await _breakdown_region_manager(
            db, org_id, membership.region_id,
            getattr(membership, "program_id", None),
        )
    # branch_manager / coach / viewer — no breakdown surface.
    return {"bars": {"label": "", "rows": []}, "donut": None}


__all__ = ["router"]
