"""team_program_id

Phase 12 — add `program_id` to `team_profile` so a team's program is
independent of its region. Until now, program had to be inferred from
Region.program_id, which prevented one geographic region from hosting
teams across multiple programs.

The column is nullable + ON DELETE SET NULL so existing private-coach
team rows (no org, no program) are unaffected.

Revision ID: c5a8f3e1d6b4
Revises: e7d4c2a1f5b9
Create Date: 2026-05-14 22:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "c5a8f3e1d6b4"
down_revision: Union[str, Sequence[str], None] = "e7d4c2a1f5b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
    insp = inspect(bind)
    cols = _columns(insp, "team_profile")
    idxs = _indexes(insp, "team_profile")

    if "program_id" not in cols:
        # Batch mode reconstructs the table on SQLite — every constraint
        # added during the batch must be named. The explicit `name=` on the
        # FK constraint satisfies that requirement.
        with op.batch_alter_table("team_profile", recreate="auto") as batch:
            batch.add_column(
                sa.Column(
                    "program_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "programs.id",
                        ondelete="SET NULL",
                        name="fk_team_profile_program_id",
                    ),
                    nullable=True,
                )
            )

    if "idx_team_profile_program" not in idxs:
        op.create_index(
            "idx_team_profile_program", "team_profile", ["program_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    idxs = _indexes(insp, "team_profile")
    cols = _columns(insp, "team_profile")

    if "idx_team_profile_program" in idxs:
        op.drop_index("idx_team_profile_program", table_name="team_profile")
    if "program_id" in cols:
        with op.batch_alter_table("team_profile", recreate="auto") as batch:
            batch.drop_column("program_id")
