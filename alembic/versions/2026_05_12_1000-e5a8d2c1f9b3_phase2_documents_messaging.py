"""phase2_documents_messaging

Phase 2.1 — adds 6 tables for the Documents (with signature) module and the
standalone Messaging module:

1. document_templates
2. document_campaigns
3. document_deliveries
4. otp_attempts
5. messages
6. message_deliveries

All tables carry a denormalized `organization_id` for uniform Postgres RLS
isolation. `otp_attempts.organization_id` is a deviation from the master
prompt (which omitted it) — explicit decision in the Phase 2 Part A plan §5:
adds one FK, saves an RLS exception. We always know the org at OTP issue
time because parents reach the flow via a valid DocumentDelivery.

Idempotent — re-running on a partially-applied schema is safe (every
`create_table` is guarded by `inspect().has_table`).

Revision ID: e5a8d2c1f9b3
Revises: c7e1d4b9a8f2
Create Date: 2026-05-12 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "e5a8d2c1f9b3"
down_revision: Union[str, Sequence[str], None] = "c7e1d4b9a8f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# SQLite batch_alter_table requires named constraints; we don't ALTER any
# existing tables in this migration but keep the convention for parity.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Every new table gets the same RLS policy shape (matches Phase 1.1).
_PHASE2_TABLES = (
    "document_templates",
    "document_campaigns",
    "document_deliveries",
    "otp_attempts",
    "messages",
    "message_deliveries",
)


def _table_exists(insp, table: str) -> bool:
    return insp.has_table(table)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    insp = inspect(bind)

    # ---------------------------------------------------------------------
    # 1. document_templates
    # ---------------------------------------------------------------------
    if not _table_exists(insp, "document_templates"):
        op.create_table(
            "document_templates",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("category", sa.Text(), nullable=False, server_default="OTHER"),
            sa.Column("uploaded_file_url", sa.Text(), nullable=False),
            sa.Column("uploaded_file_type", sa.Text(), nullable=False),
            sa.Column("uploaded_file_size", sa.Integer(), nullable=False),
            sa.Column("form_fields", sa.Text(), nullable=True),  # JSONText → Text DDL
            sa.Column("signature_zones", sa.Text(), nullable=True),
            sa.Column(
                "requires_signature", sa.Boolean(), nullable=False, server_default=sa.text("true")
            ),
            sa.Column(
                "default_expiry_days", sa.Integer(), nullable=False, server_default="30"
            ),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column(
                "created_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index("idx_doc_templates_org", "document_templates", ["organization_id"])
        op.create_index(
            "idx_doc_templates_active",
            "document_templates",
            ["organization_id", "is_active"],
        )

    # ---------------------------------------------------------------------
    # 2. document_campaigns
    # ---------------------------------------------------------------------
    insp = inspect(bind)
    if not _table_exists(insp, "document_campaigns"):
        op.create_table(
            "document_campaigns",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "template_id",
                sa.Integer(),
                sa.ForeignKey("document_templates.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("recipient_filter", sa.Text(), nullable=False),
            sa.Column("delivery_channels", sa.Text(), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="DRAFT"),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("reminder_after_days", sa.Integer(), nullable=True, server_default="7"),
            sa.Column("total_recipients", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_signed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_opened", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "created_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index("idx_doc_campaigns_org", "document_campaigns", ["organization_id"])
        op.create_index("idx_doc_campaigns_status", "document_campaigns", ["status"])
        op.create_index("idx_doc_campaigns_template", "document_campaigns", ["template_id"])

    # ---------------------------------------------------------------------
    # 3. document_deliveries
    # ---------------------------------------------------------------------
    insp = inspect(bind)
    if not _table_exists(insp, "document_deliveries"):
        op.create_table(
            "document_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "campaign_id",
                sa.Integer(),
                sa.ForeignKey("document_campaigns.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "player_id",
                sa.Integer(),
                sa.ForeignKey("players.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "player_contact_id",
                sa.Integer(),
                sa.ForeignKey("player_contacts.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("recipient_name", sa.Text(), nullable=False),
            sa.Column("recipient_email", sa.Text(), nullable=True),
            sa.Column("recipient_phone", sa.Text(), nullable=True),
            sa.Column("unique_token", sa.Text(), nullable=False, unique=True),
            sa.Column("delivery_status", sa.Text(), nullable=False, server_default="PENDING"),
            sa.Column("channel_used", sa.Text(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("failed_reason", sa.Text(), nullable=True),
            sa.Column(
                "document_status", sa.Text(), nullable=False, server_default="NOT_OPENED"
            ),
            sa.Column("opened_at", sa.DateTime(), nullable=True),
            sa.Column("form_response", sa.Text(), nullable=True),
            sa.Column("signed_at", sa.DateTime(), nullable=True),
            sa.Column("signature_image_url", sa.Text(), nullable=True),
            sa.Column("signature_method", sa.Text(), nullable=True),
            sa.Column("final_pdf_url", sa.Text(), nullable=True),
            sa.Column("audit_data", sa.Text(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("last_reminder_sent_at", sa.DateTime(), nullable=True),
            sa.Column(
                "reminders_sent_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index(
            "idx_doc_deliveries_campaign", "document_deliveries", ["campaign_id"]
        )
        op.create_index(
            "idx_doc_deliveries_token", "document_deliveries", ["unique_token"]
        )
        op.create_index(
            "idx_doc_deliveries_player", "document_deliveries", ["player_id"]
        )
        op.create_index(
            "idx_doc_deliveries_status", "document_deliveries", ["document_status"]
        )
        op.create_index(
            "idx_doc_deliveries_org", "document_deliveries", ["organization_id"]
        )

    # ---------------------------------------------------------------------
    # 4. otp_attempts
    # ---------------------------------------------------------------------
    insp = inspect(bind)
    if not _table_exists(insp, "otp_attempts"):
        op.create_table(
            "otp_attempts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("delivery_token", sa.Text(), nullable=False),
            sa.Column("phone", sa.Text(), nullable=False),
            sa.Column("code_hash", sa.Text(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("verified_at", sa.DateTime(), nullable=True),
            sa.Column("ip_address", sa.Text(), nullable=True),
            sa.Column("user_agent", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index("idx_otp_attempts_token", "otp_attempts", ["delivery_token"])
        op.create_index("idx_otp_attempts_phone", "otp_attempts", ["phone"])
        op.create_index("idx_otp_attempts_created", "otp_attempts", ["created_at"])
        op.create_index("idx_otp_attempts_org", "otp_attempts", ["organization_id"])

    # ---------------------------------------------------------------------
    # 5. messages
    # ---------------------------------------------------------------------
    insp = inspect(bind)
    if not _table_exists(insp, "messages"):
        op.create_table(
            "messages",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("subject", sa.Text(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("recipient_filter", sa.Text(), nullable=False),
            sa.Column("delivery_channels", sa.Text(), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="DRAFT"),
            sa.Column("total_recipients", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_delivered", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "sent_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index("idx_messages_org", "messages", ["organization_id"])
        op.create_index("idx_messages_status", "messages", ["status"])

    # ---------------------------------------------------------------------
    # 6. message_deliveries
    # ---------------------------------------------------------------------
    insp = inspect(bind)
    if not _table_exists(insp, "message_deliveries"):
        op.create_table(
            "message_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "message_id",
                sa.Integer(),
                sa.ForeignKey("messages.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "player_id",
                sa.Integer(),
                sa.ForeignKey("players.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "player_contact_id",
                sa.Integer(),
                sa.ForeignKey("player_contacts.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("recipient_email", sa.Text(), nullable=True),
            sa.Column("recipient_phone", sa.Text(), nullable=True),
            sa.Column("channel_used", sa.Text(), nullable=True),
            sa.Column("delivery_status", sa.Text(), nullable=False, server_default="PENDING"),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("failed_reason", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index("idx_msg_deliveries_message", "message_deliveries", ["message_id"])
        op.create_index(
            "idx_msg_deliveries_player_contact", "message_deliveries", ["player_contact_id"]
        )
        op.create_index("idx_msg_deliveries_player", "message_deliveries", ["player_id"])
        op.create_index("idx_msg_deliveries_org", "message_deliveries", ["organization_id"])
        op.create_index(
            "idx_msg_deliveries_status", "message_deliveries", ["delivery_status"]
        )

    # ---------------------------------------------------------------------
    # 7. Postgres RLS (no-op on SQLite). Matches Phase 1.1 policy shape.
    # ---------------------------------------------------------------------
    if is_pg:
        for tbl in _PHASE2_TABLES:
            op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
            op.execute(f"DROP POLICY IF EXISTS org_isolation ON {tbl}")
            op.execute(
                f"CREATE POLICY org_isolation ON {tbl} "
                f"USING ("
                f"organization_id IS NULL OR "
                f"organization_id = current_setting('app.current_org_id', true)::int"
                f")"
            )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    insp = inspect(bind)

    # 7. Drop RLS first
    if is_pg:
        for tbl in reversed(_PHASE2_TABLES):
            op.execute(f"DROP POLICY IF EXISTS org_isolation ON {tbl}")
            op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")

    # 6 → 1: drop in reverse FK order (child tables first).
    for tbl in (
        "message_deliveries",
        "messages",
        "otp_attempts",
        "document_deliveries",
        "document_campaigns",
        "document_templates",
    ):
        insp = inspect(bind)
        if _table_exists(insp, tbl):
            # Indexes are dropped automatically with the table on both SQLite + PG.
            op.drop_table(tbl)
