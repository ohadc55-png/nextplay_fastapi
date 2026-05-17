"""org_invite_short_code

Adds the `short_code` column to `org_invites` for the human-readable
redemption flow (the inviter copies the code, sends via WhatsApp/SMS,
the invitee redeems on /org/join). One-time-use: redemption marks the
auth_token used, killing both the magic link AND the code together.

Revision ID: e7d4c2a1f5b9
Revises: b4e9c2f1d8a3
Create Date: 2026-05-14 21:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "e7d4c2a1f5b9"
down_revision: Union[str, Sequence[str], None] = "b4e9c2f1d8a3"
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

    if "short_code" not in _columns(insp, "org_invites"):
        with op.batch_alter_table("org_invites") as bop:
            bop.add_column(sa.Column("short_code", sa.Text(), nullable=True))

    insp = inspect(bind)
    existing = _indexes(insp, "org_invites")
    if "uq_org_invites_short_code" not in existing:
        # Partial unique index — pre-Phase-3 rows have NULL short_code, so
        # we MUST allow multiple NULLs. SQLite + Postgres both honor partial
        # unique indexes via the WHERE clause.
        op.create_index(
            "uq_org_invites_short_code",
            "org_invites",
            ["short_code"],
            unique=True,
            sqlite_where=sa.text("short_code IS NOT NULL"),
            postgresql_where=sa.text("short_code IS NOT NULL"),
        )
    if "idx_org_invites_short_code" not in existing:
        op.create_index(
            "idx_org_invites_short_code", "org_invites", ["short_code"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing = _indexes(insp, "org_invites")
    if "idx_org_invites_short_code" in existing:
        op.drop_index("idx_org_invites_short_code", table_name="org_invites")
    if "uq_org_invites_short_code" in existing:
        op.drop_index("uq_org_invites_short_code", table_name="org_invites")
    if "short_code" in _columns(insp, "org_invites"):
        with op.batch_alter_table("org_invites") as bop:
            bop.drop_column("short_code")
