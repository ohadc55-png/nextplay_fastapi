"""phase3_programs_hierarchy

Phase 3 — Adds the missing tier ("Program") between Organization and Region
so we can model the Sha'ar Shivyon hierarchy:

    Organization -> Program -> Region -> Team -> Coach + Players

Active roles in the post-Phase-3 system:
    org_admin, program_manager (NEW), region_manager, coach, viewer
(`branch_manager` survives in the role allowlist for Phase 0 back-compat but
is not part of the Sha'ar Shivyon tree.)

Changes:
1. Creates `programs` table (org-scoped; unique(org_id, name)).
2. Adds `regions.program_id` (NULL = legacy / org's default program).
3. Adds `user_organizations.program_id` for program_manager memberships.
4. Adds `org_invites.program_id` (so PM invites preserve scope).
5. Adds `team_profile.region_id` (teams can now live directly under a
   region without forcing a Phase 0 `branch`).
6. Postgres-only: enables RLS on `programs` mirroring the existing
   org_isolation policy.

Idempotent — each step probes via `inspect()` so it survives a re-run.
Reversible: downgrade() drops every column + table the upgrade adds.

Revision ID: d1f3a8c4e2b7
Revises: a7c93b4f1e22
Create Date: 2026-05-14 15:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from src.core.database import JSONText

revision: str = "d1f3a8c4e2b7"
down_revision: Union[str, Sequence[str], None] = "a7c93b4f1e22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _columns(insp, table: str) -> set[str]:
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _indexes(insp, table: str) -> set[str]:
    if not insp.has_table(table):
        return set()
    return {ix["name"] for ix in insp.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    insp = inspect(bind)

    # 1. programs table
    if not insp.has_table("programs"):
        op.create_table(
            "programs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("slug", sa.Text(), nullable=True),
            sa.Column("attributes_json", JSONText(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
            sa.UniqueConstraint(
                "organization_id", "name", name="uq_programs_org_name"
            ),
        )
        op.create_index("idx_programs_org", "programs", ["organization_id"])

    # 2. regions.program_id
    insp = inspect(bind)
    if "program_id" not in _columns(insp, "regions"):
        with op.batch_alter_table(
            "regions", naming_convention=_NAMING_CONVENTION
        ) as bop:
            bop.add_column(
                sa.Column(
                    "program_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "programs.id",
                        ondelete="SET NULL",
                        name="fk_regions_program_id_programs",
                    ),
                    nullable=True,
                )
            )
    insp = inspect(bind)
    if "idx_regions_program" not in _indexes(insp, "regions"):
        op.create_index("idx_regions_program", "regions", ["program_id"])

    # 3. user_organizations.program_id
    insp = inspect(bind)
    if "program_id" not in _columns(insp, "user_organizations"):
        with op.batch_alter_table(
            "user_organizations", naming_convention=_NAMING_CONVENTION
        ) as bop:
            bop.add_column(
                sa.Column(
                    "program_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "programs.id",
                        ondelete="SET NULL",
                        name="fk_user_organizations_program_id_programs",
                    ),
                    nullable=True,
                )
            )
    insp = inspect(bind)
    if "idx_user_org_program" not in _indexes(insp, "user_organizations"):
        op.create_index(
            "idx_user_org_program", "user_organizations", ["program_id"]
        )

    # 4. org_invites.program_id
    insp = inspect(bind)
    if "program_id" not in _columns(insp, "org_invites"):
        with op.batch_alter_table(
            "org_invites", naming_convention=_NAMING_CONVENTION
        ) as bop:
            bop.add_column(
                sa.Column(
                    "program_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "programs.id",
                        ondelete="SET NULL",
                        name="fk_org_invites_program_id_programs",
                    ),
                    nullable=True,
                )
            )

    # 5. team_profile.region_id
    insp = inspect(bind)
    if "region_id" not in _columns(insp, "team_profile"):
        with op.batch_alter_table(
            "team_profile", naming_convention=_NAMING_CONVENTION
        ) as bop:
            bop.add_column(
                sa.Column(
                    "region_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "regions.id",
                        ondelete="SET NULL",
                        name="fk_team_profile_region_id_regions",
                    ),
                    nullable=True,
                )
            )
    insp = inspect(bind)
    if "idx_team_profile_region" not in _indexes(insp, "team_profile"):
        op.create_index("idx_team_profile_region", "team_profile", ["region_id"])

    # 6. Postgres RLS on programs
    if is_pg:
        op.execute("ALTER TABLE programs ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE programs FORCE ROW LEVEL SECURITY")
        op.execute("DROP POLICY IF EXISTS org_isolation ON programs")
        op.execute(
            "CREATE POLICY org_isolation ON programs "
            "USING ("
            "organization_id = current_setting('app.current_org_id', true)::int"
            ")"
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    insp = inspect(bind)

    if is_pg:
        op.execute("DROP POLICY IF EXISTS org_isolation ON programs")
        op.execute("ALTER TABLE programs NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE programs DISABLE ROW LEVEL SECURITY")

    # 5. team_profile.region_id
    insp = inspect(bind)
    if "idx_team_profile_region" in _indexes(insp, "team_profile"):
        op.drop_index("idx_team_profile_region", table_name="team_profile")
    insp = inspect(bind)
    if "region_id" in _columns(insp, "team_profile"):
        with op.batch_alter_table(
            "team_profile", naming_convention=_NAMING_CONVENTION
        ) as bop:
            bop.drop_column("region_id")

    # 4. org_invites.program_id
    insp = inspect(bind)
    if "program_id" in _columns(insp, "org_invites"):
        with op.batch_alter_table(
            "org_invites", naming_convention=_NAMING_CONVENTION
        ) as bop:
            bop.drop_column("program_id")

    # 3. user_organizations.program_id
    insp = inspect(bind)
    if "idx_user_org_program" in _indexes(insp, "user_organizations"):
        op.drop_index("idx_user_org_program", table_name="user_organizations")
    insp = inspect(bind)
    if "program_id" in _columns(insp, "user_organizations"):
        with op.batch_alter_table(
            "user_organizations", naming_convention=_NAMING_CONVENTION
        ) as bop:
            bop.drop_column("program_id")

    # 2. regions.program_id
    insp = inspect(bind)
    if "idx_regions_program" in _indexes(insp, "regions"):
        op.drop_index("idx_regions_program", table_name="regions")
    insp = inspect(bind)
    if "program_id" in _columns(insp, "regions"):
        with op.batch_alter_table(
            "regions", naming_convention=_NAMING_CONVENTION
        ) as bop:
            bop.drop_column("program_id")

    # 1. programs table
    insp = inspect(bind)
    if insp.has_table("programs"):
        if "idx_programs_org" in _indexes(insp, "programs"):
            op.drop_index("idx_programs_org", table_name="programs")
        op.drop_table("programs")
