"""org_invite_team_id

Phase 14 — coach invites can pre-assign a team. Adds `team_id` to
`org_invites` so the inviter (PM/RM/admin) can pick the target team
during the invite flow, and the redeem flow assigns the new coach to
that team in one atomic step.

Nullable + ON DELETE SET NULL: deleting a team doesn't cascade-cancel
pending invites — the invite just loses its team_id and a stale invite
becomes a "team-less" coach invite (which the redeem flow handles).

Revision ID: d7b3f9a4c1e8
Revises: c5a8f3e1d6b4
Create Date: 2026-05-16 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "d7b3f9a4c1e8"
down_revision: Union[str, Sequence[str], None] = "c5a8f3e1d6b4"
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
    cols = _columns(insp, "org_invites")
    idxs = _indexes(insp, "org_invites")

    if "team_id" not in cols:
        # Batch mode for SQLite — ALTER TABLE ADD COLUMN with FK needs it.
        # The FK constraint must be named for batch-mode reconstruction.
        with op.batch_alter_table("org_invites", recreate="auto") as batch:
            batch.add_column(
                sa.Column(
                    "team_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "team_profile.id",
                        ondelete="SET NULL",
                        name="fk_org_invites_team_id",
                    ),
                    nullable=True,
                )
            )

    if "idx_org_invites_team" not in idxs:
        op.create_index(
            "idx_org_invites_team", "org_invites", ["team_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    idxs = _indexes(insp, "org_invites")
    cols = _columns(insp, "org_invites")

    if "idx_org_invites_team" in idxs:
        op.drop_index("idx_org_invites_team", table_name="org_invites")
    if "team_id" in cols:
        with op.batch_alter_table("org_invites", recreate="auto") as batch:
            batch.drop_column("team_id")
