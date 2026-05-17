"""phase3_practice_sessions

Phase 3 (continued) — adds the `practice_sessions` table used by the
"today's training" sidebar on the org_admin / program_manager /
region_manager dashboards.

Columns mirror the model in src/models/practice_sessions.py:
  - team_id NOT NULL (CASCADE)
  - user_id (coach) nullable (SET NULL on user delete)
  - organization_id / program_id / region_id denormalized (SET NULL)
  - title, scheduled_at, duration_minutes, location, status, notes,
    attributes_json
  - created_at, updated_at

Indexes:
  - (team_id), (user_id), (scheduled_at)
  - (organization_id, scheduled_at) — primary sidebar query
  - (program_id,    scheduled_at)
  - (region_id,     scheduled_at)

Postgres only: enables RLS with the same org_isolation policy used on
other org-scoped tables. SQLite is a no-op.

Idempotent — guarded by `inspect().has_table`.

Revision ID: b4e9c2f1d8a3
Revises: d1f3a8c4e2b7
Create Date: 2026-05-14 17:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from src.core.database import JSONText

revision: str = "b4e9c2f1d8a3"
down_revision: Union[str, Sequence[str], None] = "d1f3a8c4e2b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _indexes(insp, table: str) -> set[str]:
    if not insp.has_table(table):
        return set()
    return {ix["name"] for ix in insp.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    insp = inspect(bind)

    if not insp.has_table("practice_sessions"):
        op.create_table(
            "practice_sessions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "team_id",
                sa.Integer(),
                sa.ForeignKey("team_profile.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "program_id",
                sa.Integer(),
                sa.ForeignKey("programs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "region_id",
                sa.Integer(),
                sa.ForeignKey("regions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("scheduled_at", sa.DateTime(), nullable=False),
            sa.Column("duration_minutes", sa.Integer(), nullable=True),
            sa.Column("location", sa.Text(), nullable=True),
            sa.Column(
                "status", sa.Text(), nullable=False, server_default="scheduled"
            ),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("attributes_json", JSONText(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    insp = inspect(bind)
    existing = _indexes(insp, "practice_sessions")
    if "idx_practice_team" not in existing:
        op.create_index("idx_practice_team", "practice_sessions", ["team_id"])
    if "idx_practice_user" not in existing:
        op.create_index("idx_practice_user", "practice_sessions", ["user_id"])
    if "idx_practice_scheduled" not in existing:
        op.create_index(
            "idx_practice_scheduled", "practice_sessions", ["scheduled_at"]
        )
    if "idx_practice_org_scheduled" not in existing:
        op.create_index(
            "idx_practice_org_scheduled",
            "practice_sessions",
            ["organization_id", "scheduled_at"],
        )
    if "idx_practice_program_scheduled" not in existing:
        op.create_index(
            "idx_practice_program_scheduled",
            "practice_sessions",
            ["program_id", "scheduled_at"],
        )
    if "idx_practice_region_scheduled" not in existing:
        op.create_index(
            "idx_practice_region_scheduled",
            "practice_sessions",
            ["region_id", "scheduled_at"],
        )

    if is_pg:
        op.execute("ALTER TABLE practice_sessions ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE practice_sessions FORCE ROW LEVEL SECURITY")
        op.execute("DROP POLICY IF EXISTS org_isolation ON practice_sessions")
        # NULL org = private-coach practice → exempt from policy (same shape
        # as players policy from c7e1d4b9a8f2).
        op.execute(
            "CREATE POLICY org_isolation ON practice_sessions "
            "USING ("
            "organization_id IS NULL OR "
            "organization_id = current_setting('app.current_org_id', true)::int"
            ")"
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    insp = inspect(bind)

    if is_pg:
        op.execute("DROP POLICY IF EXISTS org_isolation ON practice_sessions")
        op.execute("ALTER TABLE practice_sessions NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE practice_sessions DISABLE ROW LEVEL SECURITY")

    if insp.has_table("practice_sessions"):
        for ix in (
            "idx_practice_region_scheduled",
            "idx_practice_program_scheduled",
            "idx_practice_org_scheduled",
            "idx_practice_scheduled",
            "idx_practice_user",
            "idx_practice_team",
        ):
            if ix in _indexes(insp, "practice_sessions"):
                op.drop_index(ix, table_name="practice_sessions")
        op.drop_table("practice_sessions")
