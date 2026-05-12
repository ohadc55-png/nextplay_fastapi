"""Demo tenant seeder — the actual building blocks.

The CLI lives in `scripts/seed_enterprise_demo.py`; this module exists so
the seeding logic is importable from anywhere on the regular `src.*` path
(tests, services, future admin endpoints) without the path-mangling shim
the script needs to run standalone.

Phase 1.2 — produces a representative Sha'ar Shivyon-shaped tenant:
6 regions × ~20 branches × ~5 teams × ~10 players  ≈  600 teams, 6,000 players.

Performance trick: bcrypt-12 takes ~250ms per call. Hashing 700+ users would
add ~3 minutes. We hash ONE shared demo password once and reuse the hash for
every demo user — the only practical way to keep the seeder under 30s.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.password_service import hash_password
from src.models.branches import Branch
from src.models.organizations import Organization
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.regions import Region
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.models.users import User

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Demo data — inline so this module is self-contained
# ---------------------------------------------------------------------------

DEFAULT_SLUG = "shaar-shivyon"
DEFAULT_NAME = "שער שיוויון"
DEMO_PASSWORD = "demo123"
DEMO_EMAIL_DOMAIN = "demo.nextplay.local"

HEBREW_REGIONS: list[str] = [
    "מחוז צפון",
    "מחוז חיפה",
    "מחוז מרכז",
    "מחוז ירושלים",
    "מחוז דרום",
    "מחוז שפלה",
]

# Geographic mapping: 20 branch names per region, real Israeli localities
# grouped to match each region. Used by:
#   1. The seeder, so a fresh `--reset` produces consistent geography.
#   2. scripts/rename_branches.py, which renames existing branches in-place
#      without touching teams/players (non-destructive).
HEBREW_BRANCHES_BY_REGION: dict[str, list[str]] = {
    "מחוז צפון": [
        "קרית שמונה", "מטולה", "נהריה", "מעלות-תרשיחא", "צפת", "ראש פינה",
        "חצור הגלילית", "קצרין", "טבריה", "יבניאל", "יסוד המעלה", "שלומי",
        "כפר ורדים", "בית שאן", "עפולה", "מגדל העמק", "נצרת", "נצרת עילית",
        "יקנעם", "ראש העין הגלילית",
    ],
    "מחוז חיפה": [
        "חיפה 1 (כרמל)", "חיפה 2 (הדר)", "חיפה 3 (נווה שאנן)", "טבעון", "נשר",
        "קרית ביאליק", "קרית מוצקין", "קרית ים", "קרית אתא", "רכסים",
        "עכו", "כרמיאל", "זכרון יעקב", "בנימינה", "פרדס חנה",
        "חדרה", "אור עקיבא", "עתלית", "דליית אל כרמל", "עוספיה",
    ],
    "מחוז מרכז": [
        "תל אביב 1 (צפון)", "תל אביב 2 (מרכז)", "תל אביב 3 (יפו)", "רמת גן",
        "גבעתיים", "בני ברק", "פתח תקווה", "חולון", "בת ים", "הרצליה",
        "רעננה", "כפר סבא", "הוד השרון", "רמת השרון", "אור יהודה",
        "יהוד-מונוסון", "קרית אונו", "גני תקווה", "פרדס כץ", "אזור",
        "אלעד", "סביון", "גבעת שמואל", "גני יהודה", "כפר שמריהו",
        "רמת אפעל", "נווה אפק", "ניר אליהו",
    ],
    "מחוז ירושלים": [
        "ירושלים 1 (מרכז)", "ירושלים 2 (גילה)", "ירושלים 3 (פסגת זאב)",
        "ירושלים 4 (מערב)", "מעלה אדומים", "מבשרת ציון", "גבעת זאב",
        "ביתר עילית", "בית שמש 1", "בית שמש 2", "אבו גוש", "צור הדסה",
        "אפרת", "הר אדר", "קרית יערים", "רמת שלמה", "גוש עציון",
        "אורנית", "אריאל", "מודיעין עילית",
    ],
    "מחוז דרום": [
        "באר שבע 1", "באר שבע 2", "באר שבע 3", "אילת", "דימונה", "ערד",
        "ירוחם", "נתיבות", "אופקים", "שדרות", "אשקלון 1", "אשקלון 2",
        "מצפה רמון", "אורן", "מיתר", "להבים", "רהט", "תל שבע", "עומר", "להב",
    ],
    "מחוז שפלה": [
        "ראשון לציון 1", "ראשון לציון 2", "רחובות", "נס ציונה", "רמלה", "לוד",
        "מודיעין 1", "מודיעין 2", "יבנה", "גדרה", "גן יבנה",
        "אשדוד 1", "אשדוד 2", "אשדוד 3", "קרית מלאכי", "קרית גת",
        "חבל יבנה", "מזכרת בתיה", "כפר חב\"ד", "רחובות 2",
    ],
}

# Legacy flat list — kept only because some old tests reference it. New
# branch naming should use HEBREW_BRANCHES_BY_REGION.
HEBREW_CITIES: list[str] = [
    "תל אביב", "ירושלים", "חיפה", "באר שבע", "נתניה", "אשדוד", "ראשון לציון",
    "פתח תקווה", "חולון", "רחובות", "רמת גן", "כפר סבא", "הרצליה", "מודיעין",
    "אשקלון", "נצרת", "בית שמש", "בני ברק", "רעננה", "גבעתיים", "קריית גת",
    "אילת", "עפולה", "טבריה", "צפת", "כרמיאל", "דימונה", "ערד", "לוד",
    "רמלה", "אילון", "כפר ורדים", "פרדס חנה", "זכרון יעקב",
]

COACH_FIRST_HE: list[str] = [
    "אבי", "יוסי", "דוד", "משה", "אורי", "אסף", "אילן", "תומר", "רוני",
    "אריאל", "ניר", "עידן", "גיא", "אורן", "יואב", "עומר", "אלון", "שי",
    "רן", "אופיר", "מוטי", "אייל", "דני", "אורלי", "מיכל", "ענת",
    "תמר", "נועה", "שרון", "ליאת", "עינת", "הילה", "רונית", "שירה",
]

COACH_LAST_HE: list[str] = [
    "כהן", "לוי", "מזרחי", "פרץ", "ביטון", "אברהם", "פרידמן", "אזולאי",
    "דהן", "אוחיון", "חדד", "סויסה", "אטיאס", "מלכה", "שוורץ", "כץ",
    "רוזנברג", "גולן", "סבן", "אביטל", "ברק", "שמש", "אסולין",
]

PLAYER_FIRST_HE: list[str] = [
    "איתי", "נדב", "ליאם", "אורי", "אריאל", "אדם", "יואב", "רועי", "ניב",
    "אגם", "עמית", "ידידיה", "אופק", "אריק", "אייתן", "אלכס", "בן", "גל",
    "דניאל", "הראל", "יהונתן", "יותם", "מתן", "סהר", "עידו", "רון", "שגיא",
    "תום", "אריה", "אוהד", "אסף",
]

POSITIONS: list[str] = ["PG", "SG", "SF", "PF", "C"]
LEAGUES: list[str] = ["ליגה ארצית נוער", "ליגת על נוער", "ליגה א'", "ליגה ב'", "מועדון"]
DIVISIONS: list[str] = ["U12", "U14", "U16", "U18", "בוגרים"]


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------

@dataclass
class RegionConfig:
    """Per-region totals. Branches are even (each branch has a curated geo
    name), but team counts within a region get spread across the region's
    branches with a random weighted distribution so no two branches end up
    with exactly the same number of teams."""
    name: str
    branches: int
    teams: int


# Realistic varied distribution roughly modelled on Israeli basketball
# density: Tel Aviv ("מרכז") is densest, the periphery ("דרום", "צפון")
# is sparser. Totals: 110 branches, 548 teams, ~6,000 players (avg ~11/team).
REGION_CONFIGS_DEFAULT: list[RegionConfig] = [
    RegionConfig(name="מחוז צפון",    branches=14, teams=58),
    RegionConfig(name="מחוז חיפה",    branches=18, teams=88),
    RegionConfig(name="מחוז מרכז",    branches=28, teams=168),
    RegionConfig(name="מחוז ירושלים", branches=21, teams=108),
    RegionConfig(name="מחוז דרום",    branches=12, teams=48),
    RegionConfig(name="מחוז שפלה",    branches=17, teams=78),
]


# Multi-team-coach distribution. Cumulative thresholds: ~70% of coaches have
# 1 team, 22% have 2, 8% have 3. Avg ~1.38 teams/coach. Reflects reality —
# most basketball clubs have head coaches who run U12+U14, or U16+U18.
COACH_MULTI_TEAM_WEIGHTS: list[tuple[float, int]] = [
    (0.70, 1),
    (0.92, 2),
    (1.00, 3),
]


@dataclass
class SeedConfig:
    # Legacy uniform fields (preserved so existing tests + CLI numeric flags
    # keep working unchanged). If `region_configs` is None we fall back to the
    # uniform behaviour driven by these fields.
    regions: int = 6
    branches_per_region: int = 20
    teams_per_branch: int = 5
    players_per_team: int = 10
    contacts_per_team: int = 3  # N players per team get an encrypted contact row

    # New Phase 1.2-r — varied geography / multi-coach demo.
    region_configs: list[RegionConfig] | None = None
    multi_team_coach: bool = False
    # If set, players_per_team becomes random within [min, max] inclusive.
    players_per_team_range: tuple[int, int] | None = None

    def resolve_region_configs(self) -> list[RegionConfig]:
        """Use explicit per-region configs when given; otherwise synthesize a
        uniform list from the legacy fields (so tests + small CLI runs keep
        their old shape)."""
        if self.region_configs is not None:
            return self.region_configs
        names = (HEBREW_REGIONS * ((self.regions // len(HEBREW_REGIONS)) + 1))[: self.regions]
        return [
            RegionConfig(
                name=n,
                branches=self.branches_per_region,
                teams=self.branches_per_region * self.teams_per_branch,
            )
            for n in names
        ]

    @property
    def total_branches(self) -> int:
        return sum(rc.branches for rc in self.resolve_region_configs())

    @property
    def total_teams(self) -> int:
        return sum(rc.teams for rc in self.resolve_region_configs())

    @property
    def total_players(self) -> int:
        if self.players_per_team_range:
            avg = sum(self.players_per_team_range) / 2
        else:
            avg = self.players_per_team
        return int(self.total_teams * avg)

    @property
    def total_users(self) -> int:
        # Rough estimate; actual coach count depends on multi_team_coach (~1.38).
        coach_estimate = (
            int(self.total_teams / 1.38) if self.multi_team_coach else self.total_teams
        )
        return (
            1
            + len(self.resolve_region_configs())
            + self.total_branches
            + coach_estimate
        )


def _distribute_int(
    total: int, buckets: int, rng: random.Random, *, min_per_bucket: int = 1
) -> list[int]:
    """Randomly distribute `total` units across `buckets`, with each bucket
    getting at least `min_per_bucket`. The remainder is sprinkled one-at-a-time
    into random buckets, producing natural variance (some branches end up with
    2 teams, others with 9) instead of a flat per-branch quotient."""
    if buckets <= 0:
        return []
    if buckets * min_per_bucket > total:
        # Caller asked for an impossible floor — degrade gracefully.
        base = total // buckets
        counts = [base] * buckets
        for i in range(total - base * buckets):
            counts[i] += 1
        return counts
    counts = [min_per_bucket] * buckets
    for _ in range(total - buckets * min_per_bucket):
        counts[rng.randrange(buckets)] += 1
    return counts


def _coach_group_sizes(num_teams: int, rng: random.Random) -> list[int]:
    """Decide how the teams in a single branch are partitioned among coaches.
    Each group of size N means one coach takes N consecutive teams in that
    branch. Sums to `num_teams`."""
    groups: list[int] = []
    remaining = num_teams
    while remaining > 0:
        r = rng.random()
        size = 1
        for threshold, multiplicity in COACH_MULTI_TEAM_WEIGHTS:
            if r < threshold:
                size = multiplicity
                break
        size = min(size, remaining)
        groups.append(size)
        remaining -= size
    return groups


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

async def cleanup_demo_tenant(session: AsyncSession, slug: str) -> bool:
    """Delete every row tied to the demo org. Returns True if anything was
    deleted. Idempotent — running on a clean DB is a no-op.

    Refuses to delete an org whose `attributes_json.seed_origin` is not
    `'demo'` — guards against accidentally wiping a real customer."""
    org = (await session.execute(
        select(Organization).where(Organization.slug == slug)
    )).scalar_one_or_none()
    if org is None:
        return False

    if (org.attributes_json or {}).get("seed_origin") != "demo":
        raise RuntimeError(
            f"Refusing to delete org id={org.id} slug={slug!r}: "
            f"its attributes_json.seed_origin is not 'demo'."
        )

    org_id = org.id

    # Order matters because some FKs are SET NULL (not CASCADE):
    #  - players, team_profile point at organizations via SET NULL
    #  - player_contacts cascades from players
    # Easiest correct order: child tables first.
    await session.execute(
        text("DELETE FROM player_contacts WHERE organization_id = :id"),
        {"id": org_id},
    )
    await session.execute(
        text("DELETE FROM players WHERE organization_id = :id"),
        {"id": org_id},
    )
    await session.execute(
        text("DELETE FROM team_profile WHERE organization_id = :id"),
        {"id": org_id},
    )
    # org_audit_logs uses ondelete=RESTRICT (audit trail must outlive cascades
    # of real customer data). For demo cleanup that's the wrong policy — wipe
    # them explicitly.
    await session.execute(
        text("DELETE FROM org_audit_logs WHERE organization_id = :id"),
        {"id": org_id},
    )

    # Demo users (identified by email domain + memberships).
    demo_user_ids = [
        r[0] for r in (
            await session.execute(text(
                "SELECT DISTINCT u.id FROM users u "
                "JOIN user_organizations uo ON uo.user_id = u.id "
                "WHERE uo.organization_id = :id "
                f"AND u.email LIKE '%@{DEMO_EMAIL_DOMAIN}'"
            ), {"id": org_id})
        ).all()
    ]

    # user_organizations + org_audit_logs + org_invites cascade from organizations
    # (per Phase 0 schema). Deleting the org will sweep them. To also remove
    # the orphaned demo users, do it manually with an ORM delete (`IN (...)`
    # via the ORM expands the list correctly on both SQLite and Postgres).
    await session.delete(org)
    await session.flush()

    if demo_user_ids:
        await session.execute(
            delete(User).where(User.id.in_(demo_user_ids))
        )

    await session.commit()
    log.info(
        "cleaned org id=%d + %d demo users", org_id, len(demo_user_ids)
    )
    return True


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

async def seed_demo_tenant(
    session: AsyncSession, slug: str, name: str, cfg: SeedConfig,
) -> dict:
    """Create the demo tenant. Returns a counts dict."""
    region_cfgs = cfg.resolve_region_configs()
    log.info(
        "Seeding demo tenant slug=%r (%d regions, %d branches, %d teams, ~%d players, multi_team_coach=%s)…",
        slug, len(region_cfgs), cfg.total_branches, cfg.total_teams,
        cfg.total_players, cfg.multi_team_coach,
    )

    t0 = time.time()
    rng = random.Random(42)  # deterministic across runs for diff'ability
    demo_pw_hash = hash_password(DEMO_PASSWORD)

    # ---- Org admin user + org row ----
    org_admin = User(
        email=f"admin@{DEMO_EMAIL_DOMAIN}",
        password_hash=demo_pw_hash,
        display_name="Demo Org Admin",
        email_verified=True,
    )
    session.add(org_admin)
    await session.flush()

    org = Organization(
        slug=slug,
        name=name,
        status="active",
        plan="enterprise",
        legal_name="עמותת שער שיוויון (ע.ר.)",
        primary_color="#2563EB",
        structure_type="regions_branches",
        monthly_fee_cents=500_000,
        setup_fee_cents=1_000_000,
        created_by=org_admin.id,
        attributes_json={
            "seed_origin": "demo",
            "seeded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    session.add(org)
    await session.flush()

    session.add(UserOrganization(
        user_id=org_admin.id,
        organization_id=org.id,
        role="org_admin",
        status="active",
        accepted_at=datetime.now(UTC),
    ))

    # ---- Regions ----
    region_objs: list[Region] = [
        Region(organization_id=org.id, name=rc.name) for rc in region_cfgs
    ]
    session.add_all(region_objs)
    await session.flush()

    # Region managers (1 per region)
    region_managers: list[User] = []
    for i, _ in enumerate(region_objs):
        region_managers.append(User(
            email=f"region{i + 1}@{DEMO_EMAIL_DOMAIN}",
            password_hash=demo_pw_hash,
            display_name=f"{rng.choice(COACH_FIRST_HE)} {rng.choice(COACH_LAST_HE)}",
            email_verified=True,
        ))
    session.add_all(region_managers)
    await session.flush()
    for rm, region in zip(region_managers, region_objs, strict=False):
        session.add(UserOrganization(
            user_id=rm.id, organization_id=org.id, role="region_manager",
            region_id=region.id, status="active",
            accepted_at=datetime.now(UTC),
        ))

    # ---- Branches ----
    branch_objs: list[Branch] = []
    branches_by_region: dict[int, list[Branch]] = {}
    for region, rc in zip(region_objs, region_cfgs, strict=False):
        curated = HEBREW_BRANCHES_BY_REGION.get(rc.name, [])
        region_branches: list[Branch] = []
        for j in range(rc.branches):
            b_name = curated[j] if j < len(curated) else f"{rc.name} {j + 1}"
            b = Branch(organization_id=org.id, region_id=region.id, name=b_name)
            region_branches.append(b)
        branches_by_region[region.id] = region_branches
        branch_objs.extend(region_branches)
    session.add_all(branch_objs)
    await session.flush()

    # Branch managers (1 per branch)
    branch_managers: list[User] = []
    for i, _ in enumerate(branch_objs):
        branch_managers.append(User(
            email=f"branch{i + 1}@{DEMO_EMAIL_DOMAIN}",
            password_hash=demo_pw_hash,
            display_name=f"{rng.choice(COACH_FIRST_HE)} {rng.choice(COACH_LAST_HE)}",
            email_verified=True,
        ))
    session.add_all(branch_managers)
    await session.flush()
    for bm, branch in zip(branch_managers, branch_objs, strict=False):
        session.add(UserOrganization(
            user_id=bm.id, organization_id=org.id, role="branch_manager",
            region_id=branch.region_id, branch_id=branch.id, status="active",
            accepted_at=datetime.now(UTC),
        ))

    # ---- Teams: distribute each region's `teams` budget across its branches.
    # Each branch gets at least 1 team. The remainder is sprinkled randomly so
    # one branch may end up with 9 teams while a neighbour has 2.
    teams: list[TeamProfile] = []
    teams_by_branch: dict[int, list[TeamProfile]] = {}
    for region, rc in zip(region_objs, region_cfgs, strict=False):
        region_branches = branches_by_region[region.id]
        if not region_branches:
            continue
        team_counts = _distribute_int(rc.teams, len(region_branches), rng, min_per_bucket=1)
        for branch, n_teams in zip(region_branches, team_counts, strict=False):
            branch_teams: list[TeamProfile] = []
            for _ in range(n_teams):
                t = TeamProfile(
                    user_id=None,  # set after coaches are created
                    organization_id=org.id,
                    branch_id=branch.id,
                    team_name=f"{rng.choice(DIVISIONS)} - {branch.name.split()[-1]}",
                    league=rng.choice(LEAGUES),
                    division=rng.choice(DIVISIONS),
                )
                branch_teams.append(t)
                teams.append(t)
            teams_by_branch[branch.id] = branch_teams

    # ---- Coaches: partition each branch's teams into coach groups (1-3 teams
    # per coach if multi_team_coach is enabled; else 1-1).
    coach_users: list[User] = []
    # Map team object identity (Python id() — works pre-flush) -> coach index.
    team_to_coach_idx: dict[int, int] = {}
    coach_idx_counter = 0
    for branch in branch_objs:
        branch_teams = teams_by_branch.get(branch.id, [])
        if not branch_teams:
            continue
        groups = (
            _coach_group_sizes(len(branch_teams), rng)
            if cfg.multi_team_coach else [1] * len(branch_teams)
        )
        cursor = 0
        for group_size in groups:
            coach = User(
                email=f"coach{coach_idx_counter + 1}@{DEMO_EMAIL_DOMAIN}",
                password_hash=demo_pw_hash,
                display_name=f"{rng.choice(COACH_FIRST_HE)} {rng.choice(COACH_LAST_HE)}",
                email_verified=True,
            )
            coach_users.append(coach)
            for offset in range(group_size):
                team_to_coach_idx[id(branch_teams[cursor + offset])] = coach_idx_counter
            cursor += group_size
            coach_idx_counter += 1
    session.add_all(coach_users)
    await session.flush()

    # Now we know the coach IDs — stamp team.user_id and persist teams.
    for team in teams:
        team.user_id = coach_users[team_to_coach_idx[id(team)]].id
    session.add_all(teams)
    await session.flush()

    # One UserOrganization row per (coach, branch). A multi-team coach in the
    # same branch needs only one row; this is naturally deduped below.
    seen_coach_branch: set[tuple[int, int]] = set()
    for team in teams:
        key = (team.user_id, team.branch_id)
        if key in seen_coach_branch:
            continue
        seen_coach_branch.add(key)
        session.add(UserOrganization(
            user_id=team.user_id, organization_id=org.id, role="coach",
            branch_id=team.branch_id, status="active",
            accepted_at=datetime.now(UTC),
        ))

    # ---- Players + contacts: players per team is varied if a range is set;
    # else fixed at cfg.players_per_team (legacy uniform behaviour).
    players: list[Player] = []
    contacts: list[PlayerContact] = []
    for team in teams:
        if cfg.players_per_team_range:
            n_players = rng.randint(*cfg.players_per_team_range)
        else:
            n_players = cfg.players_per_team
        for n in range(n_players):
            full = f"{rng.choice(PLAYER_FIRST_HE)} {rng.choice(COACH_LAST_HE)}"
            players.append(Player(
                user_id=team.user_id,
                team_id=team.id,
                organization_id=org.id,
                name=full,
                number=n + 4,
                position=rng.choice(POSITIONS),
                age=rng.randint(12, 18),
                active=True,
            ))
    session.add_all(players)
    await session.flush()

    for team in teams:
        team_players = [p for p in players if p.team_id == team.id]
        for p in team_players[: cfg.contacts_per_team]:
            contacts.append(PlayerContact(
                player_id=p.id,
                organization_id=org.id,
                parent_name=f"{rng.choice(COACH_FIRST_HE)} {p.name.split()[-1]}",
                parent_email=f"parent{p.id}@{DEMO_EMAIL_DOMAIN}",
                parent_phone_enc=f"050-{rng.randint(1000000, 9999999)}",
                national_id_enc=f"{rng.randint(100000000, 399999999)}",
                medical_notes_enc=rng.choice(["", "אסטמה", "אלרגיה לבוטנים", ""]),
                address_enc=f"רחוב {rng.choice(['הרצל', 'בן גוריון', 'ויצמן'])} "
                            f"{rng.randint(1, 99)}",
            ))
    session.add_all(contacts)
    await session.commit()

    elapsed = time.time() - t0

    return {
        "org_id": org.id,
        "regions": len(region_objs),
        "branches": len(branch_objs),
        "teams": len(teams),
        "players": len(players),
        "contacts": len(contacts),
        "users": 1 + len(region_managers) + len(branch_managers) + len(coach_users),
        "elapsed_s": round(elapsed, 2),
    }


__all__ = [
    "DEFAULT_NAME",
    "DEFAULT_SLUG",
    "DEMO_EMAIL_DOMAIN",
    "DEMO_PASSWORD",
    "REGION_CONFIGS_DEFAULT",
    "RegionConfig",
    "SeedConfig",
    "cleanup_demo_tenant",
    "seed_demo_tenant",
]
