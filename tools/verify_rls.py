"""Smoke-test for the Phase 0 PostgreSQL Row Level Security policies.

Layer 3 of the multi-org tenancy defense — RLS — cannot be exercised on
SQLite (test runtime). This standalone script seeds two orgs + a team in
each, then issues SELECTs scoped via `set_config('app.current_org_id', ...)`
and asserts that each org's session sees ONLY its own rows.

Run against a Postgres database that has the Phase 0 migrations applied.

Usage:
    set DATABASE_URL=postgresql://localhost/nextplay
    python tools/verify_rls.py

Exit codes:
    0 — all scenarios PASS (cross-org isolation enforced)
    1 — any scenario FAIL (cross-org leak or empty when expected non-empty)
    2 — Postgres not reachable / migration not applied
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Any

try:
    import asyncpg  # type: ignore
except ImportError:
    print("FAIL: asyncpg is not installed; run `pip install asyncpg`", file=sys.stderr)
    sys.exit(2)


def _resolve_dsn() -> str:
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        print(
            "FAIL: DATABASE_URL is not set. Point it at a Postgres DB "
            "with the Phase 0 migrations applied.",
            file=sys.stderr,
        )
        sys.exit(2)
    # Strip the SQLAlchemy `+asyncpg` driver tag if present.
    if raw.startswith("postgresql+asyncpg://"):
        raw = raw.replace("postgresql+asyncpg://", "postgresql://", 1)
    elif raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql://", 1)
    return raw


async def _seed(conn: asyncpg.Connection) -> tuple[int, int, int, int]:
    """Insert 2 orgs + 1 team in each. Returns (org1_id, org2_id, team1_id, team2_id).
    Bypasses RLS by using a session with no `app.current_org_id` set
    (the policies' USING clause evaluates to NULL → row excluded; INSERTs
    are not gated by USING but by WITH CHECK, which we did not add — so
    inserts go through)."""
    suffix = uuid.uuid4().hex[:8]
    org1_id = await conn.fetchval(
        "INSERT INTO organizations (slug, name, status, plan) VALUES ($1, $2, 'active', 'enterprise') RETURNING id",
        f"rls-org1-{suffix}", "RLS Org 1",
    )
    org2_id = await conn.fetchval(
        "INSERT INTO organizations (slug, name, status, plan) VALUES ($1, $2, 'active', 'enterprise') RETURNING id",
        f"rls-org2-{suffix}", "RLS Org 2",
    )
    team1_id = await conn.fetchval(
        "INSERT INTO team_profile (team_name, organization_id) VALUES ($1, $2) RETURNING id",
        f"team-rls-1-{suffix}", org1_id,
    )
    team2_id = await conn.fetchval(
        "INSERT INTO team_profile (team_name, organization_id) VALUES ($1, $2) RETURNING id",
        f"team-rls-2-{suffix}", org2_id,
    )
    return org1_id, org2_id, team1_id, team2_id


async def _scenario(
    conn: asyncpg.Connection,
    *,
    label: str,
    org_id: int | None,
    team_id_visible: int | None,
    team_id_hidden: int | None,
) -> bool:
    """Run a single isolation check inside a single transaction (so SET
    LOCAL is scoped + auto-cleared). Returns True if the assertions pass."""
    async with conn.transaction():
        if org_id is not None:
            await conn.execute("SELECT set_config('app.current_org_id', $1, true)", str(org_id))
        else:
            # Explicitly clear (transaction-local).
            await conn.execute("RESET app.current_org_id")

        rows = await conn.fetch("SELECT id, organization_id FROM team_profile")
        ids: set[int] = {r["id"] for r in rows}

    visible_ok = (team_id_visible is None) or (team_id_visible in ids)
    hidden_ok = (team_id_hidden is None) or (team_id_hidden not in ids)
    status = "PASS" if visible_ok and hidden_ok else "FAIL"
    print(
        f"[{status}] {label} | seen={sorted(ids)} | expect_visible={team_id_visible} expect_hidden={team_id_hidden}"
    )
    return visible_ok and hidden_ok


async def _main() -> int:
    dsn = _resolve_dsn()
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:
        print(f"FAIL: could not connect to Postgres at {dsn!r}: {exc}", file=sys.stderr)
        return 2

    try:
        # Verify the policy actually exists. If not, the migration hasn't run.
        policy_row = await conn.fetchrow(
            "SELECT policyname FROM pg_policies "
            "WHERE schemaname = 'public' AND tablename = 'team_profile' AND policyname = 'org_isolation'"
        )
        if policy_row is None:
            print(
                "FAIL: org_isolation policy is missing on team_profile. "
                "Run `alembic upgrade head` against this DB first.",
                file=sys.stderr,
            )
            return 2

        org1, org2, t1, t2 = await _seed(conn)
        print(f"Seeded org1={org1} (team={t1}), org2={org2} (team={t2})")

        all_pass = True
        all_pass &= await _scenario(
            conn, label="org1 sees only its team",
            org_id=org1, team_id_visible=t1, team_id_hidden=t2,
        )
        all_pass &= await _scenario(
            conn, label="org2 sees only its team",
            org_id=org2, team_id_visible=t2, team_id_hidden=t1,
        )
        all_pass &= await _scenario(
            conn, label="nonexistent org sees nothing",
            org_id=999_999_999, team_id_visible=None, team_id_hidden=t1,
        )

        # Cleanup the seeded rows so reruns don't accumulate.
        async with conn.transaction():
            await conn.execute("RESET app.current_org_id")
            await conn.execute("DELETE FROM team_profile WHERE id IN ($1, $2)", t1, t2)
            await conn.execute("DELETE FROM organizations WHERE id IN ($1, $2)", org1, org2)

        if not all_pass:
            print("FAIL: at least one RLS scenario did NOT enforce isolation.", file=sys.stderr)
            return 1
        print("OK: RLS isolation verified across 3 scenarios.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
