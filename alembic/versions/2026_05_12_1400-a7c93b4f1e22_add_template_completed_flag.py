"""add_template_completed_flag

Phase 2.5b — manual "completed" toggle on document_templates. Distinct from
soft-delete (`is_active`): a completed template stays visible but renders
struck through, sorts to the bottom, and disables send/edit/delete. The
toggle is reversible. `completed_at` captures the timestamp for audit.

Idempotent — guarded by `inspect().has_column`.

Revision ID: a7c93b4f1e22
Revises: e5a8d2c1f9b3
Create Date: 2026-05-12 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "a7c93b4f1e22"
down_revision: Union[str, Sequence[str], None] = "e5a8d2c1f9b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    insp = inspect(bind)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def upgrade() -> None:
    cols = _existing_columns("document_templates")
    if "is_completed" not in cols:
        op.add_column(
            "document_templates",
            sa.Column(
                "is_completed", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if "completed_at" not in cols:
        op.add_column(
            "document_templates",
            sa.Column("completed_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    cols = _existing_columns("document_templates")
    if "completed_at" in cols:
        op.drop_column("document_templates", "completed_at")
    if "is_completed" in cols:
        op.drop_column("document_templates", "is_completed")
