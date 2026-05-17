"""phase15_calendar

Phase 15 — Coach Calendar. All-additive migration that wires three
existing tables for the new calendar surface:

  * team_profile  — color_hex (palette chip), ical_token / share_token
    (per-team opaque credentials for the iCal feed + public share page).
  * practice_sessions — practice_plan_entry_id (link to a NotebookEntry
    of type 'practice_plan' attached to this event) + parent_event_id
    (self-FK for "edit this occurrence only" semantics in a series).
  * notebook_entries — practice_session_id (reverse link from an
    attendance / free_document entry back to the event it describes).

Every new column is NULLABLE. No drops, no renames, no NOT NULL
constraints — safe on a populated production DB. Existing flows that
don't know about these columns continue to work unchanged.

Revision ID: f8e2c9a7b3d5
Revises: d7b3f9a4c1e8
Create Date: 2026-05-17 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "f8e2c9a7b3d5"
down_revision: Union[str, Sequence[str], None] = "d7b3f9a4c1e8"
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

    # --- team_profile -----------------------------------------------------
    tp_cols = _columns(insp, "team_profile")
    if "color_hex" not in tp_cols:
        with op.batch_alter_table("team_profile", recreate="auto") as batch:
            batch.add_column(sa.Column("color_hex", sa.Text(), nullable=True))

    if "ical_token" not in tp_cols:
        with op.batch_alter_table("team_profile", recreate="auto") as batch:
            batch.add_column(sa.Column("ical_token", sa.Text(), nullable=True))
            batch.create_unique_constraint("uq_team_profile_ical_token", ["ical_token"])

    if "share_token" not in tp_cols:
        with op.batch_alter_table("team_profile", recreate="auto") as batch:
            batch.add_column(sa.Column("share_token", sa.Text(), nullable=True))
            batch.create_unique_constraint("uq_team_profile_share_token", ["share_token"])

    # --- practice_sessions ------------------------------------------------
    ps_cols = _columns(insp, "practice_sessions")
    ps_idxs = _indexes(insp, "practice_sessions")

    if "practice_plan_entry_id" not in ps_cols:
        with op.batch_alter_table("practice_sessions", recreate="auto") as batch:
            batch.add_column(
                sa.Column(
                    "practice_plan_entry_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "notebook_entries.id",
                        ondelete="SET NULL",
                        name="fk_practice_sessions_practice_plan_entry",
                    ),
                    nullable=True,
                )
            )

    if "parent_event_id" not in ps_cols:
        with op.batch_alter_table("practice_sessions", recreate="auto") as batch:
            batch.add_column(
                sa.Column(
                    "parent_event_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "practice_sessions.id",
                        ondelete="CASCADE",
                        name="fk_practice_sessions_parent_event",
                    ),
                    nullable=True,
                )
            )

    if "idx_practice_plan_entry" not in ps_idxs:
        op.create_index(
            "idx_practice_plan_entry", "practice_sessions", ["practice_plan_entry_id"]
        )
    if "idx_practice_parent" not in ps_idxs:
        op.create_index(
            "idx_practice_parent", "practice_sessions", ["parent_event_id"]
        )

    # --- notebook_entries -------------------------------------------------
    nb_cols = _columns(insp, "notebook_entries")
    nb_idxs = _indexes(insp, "notebook_entries")

    if "practice_session_id" not in nb_cols:
        with op.batch_alter_table("notebook_entries", recreate="auto") as batch:
            batch.add_column(
                sa.Column(
                    "practice_session_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "practice_sessions.id",
                        ondelete="SET NULL",
                        name="fk_notebook_entries_practice_session",
                    ),
                    nullable=True,
                )
            )

    if "idx_nb_entries_practice_session" not in nb_idxs:
        op.create_index(
            "idx_nb_entries_practice_session",
            "notebook_entries",
            ["practice_session_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # --- notebook_entries -------------------------------------------------
    nb_idxs = _indexes(insp, "notebook_entries")
    nb_cols = _columns(insp, "notebook_entries")
    if "idx_nb_entries_practice_session" in nb_idxs:
        op.drop_index("idx_nb_entries_practice_session", table_name="notebook_entries")
    if "practice_session_id" in nb_cols:
        with op.batch_alter_table("notebook_entries", recreate="auto") as batch:
            batch.drop_column("practice_session_id")

    # --- practice_sessions ------------------------------------------------
    ps_idxs = _indexes(insp, "practice_sessions")
    ps_cols = _columns(insp, "practice_sessions")
    if "idx_practice_parent" in ps_idxs:
        op.drop_index("idx_practice_parent", table_name="practice_sessions")
    if "idx_practice_plan_entry" in ps_idxs:
        op.drop_index("idx_practice_plan_entry", table_name="practice_sessions")
    for col in ("parent_event_id", "practice_plan_entry_id"):
        if col in ps_cols:
            with op.batch_alter_table("practice_sessions", recreate="auto") as batch:
                batch.drop_column(col)

    # --- team_profile -----------------------------------------------------
    tp_cols = _columns(insp, "team_profile")
    for col in ("share_token", "ical_token", "color_hex"):
        if col in tp_cols:
            with op.batch_alter_table("team_profile", recreate="auto") as batch:
                batch.drop_column(col)
