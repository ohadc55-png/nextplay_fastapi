"""phase1_enterprise_columns

Phase 1.1 — adds the columns / table that the rest of Phase 1 depends on.

1. organizations: 12 nullable columns for the Org Creation Wizard (1.8) and
   the org-branding theme (logo_url, primary_color, subdomain) plus the
   billing/contract slots (monthly_fee_cents, setup_fee_cents, trial_ends_at,
   contract_start, contract_end). All nullable so existing Phase 0 rows
   stay valid. A partial-unique index on `subdomain WHERE NOT NULL` lets
   private-coach orgs (which never set a subdomain) coexist.

2. players: adds `organization_id` (FK + index). Backfills from
   team_profile.organization_id so existing rosters automatically inherit
   their team's org. Private-coach players (where team_profile.organization_id
   IS NULL) stay NULL. NULL-stays-NULL preserves the iron rule that nothing
   pre-Phase-1 changes.

3. player_contacts: NEW table. Two plaintext columns (parent_name,
   parent_email) for matching/search; four EncryptedText columns
   (parent_phone_enc, national_id_enc, medical_notes_enc, address_enc) so
   sensitive PII is opaque at rest. UNIQUE(player_id) keeps it 1:1.

4. Postgres RLS policies for `players` and `player_contacts` (no-op on
   SQLite). Mirrors the policy shape from 9a3f2b1c4e7d.

This migration is idempotent — every step probes via `inspect()` first so
it can be re-run after a partial failure without "duplicate column / table"
errors. (Phase 0 partially applied step 1 once; this guard keeps re-runs safe.)

Revision ID: c7e1d4b9a8f2
Revises: 9a3f2b1c4e7d
Create Date: 2026-05-11 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "c7e1d4b9a8f2"
down_revision: Union[str, Sequence[str], None] = "9a3f2b1c4e7d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# SQLite needs every constraint to have an explicit name when batch_alter_table
# rebuilds a table. The Phase 0 schema has a few `unique=True` columns without
# named constraints, so provide a naming convention here.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


_ORG_NEW_COLUMNS: list[tuple[str, sa.types.TypeEngine]] = [
    ("legal_name", sa.Text()),
    ("tax_id", sa.Text()),
    ("address", sa.Text()),
    ("logo_url", sa.Text()),
    ("primary_color", sa.Text()),
    ("subdomain", sa.Text()),
    ("structure_type", sa.Text()),
    ("monthly_fee_cents", sa.Integer()),
    ("setup_fee_cents", sa.Integer()),
    ("trial_ends_at", sa.DateTime()),
    ("contract_start", sa.Date()),
    ("contract_end", sa.Date()),
]


def _existing_columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def _existing_indexes(insp, table: str) -> set[str]:
    return {ix["name"] for ix in insp.get_indexes(table)}


def _table_exists(insp, table: str) -> bool:
    return insp.has_table(table)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    insp = inspect(bind)

    # ---------------------------------------------------------------------
    # 1. organizations — 12 nullable columns (idempotent)
    # ---------------------------------------------------------------------
    existing_org = _existing_columns(insp, "organizations")
    missing = [(n, t) for (n, t) in _ORG_NEW_COLUMNS if n not in existing_org]
    if missing:
        with op.batch_alter_table(
            "organizations", naming_convention=_NAMING_CONVENTION
        ) as bop:
            for name, type_ in missing:
                bop.add_column(sa.Column(name, type_, nullable=True))

    # Partial unique index on subdomain — re-inspect (table may have been
    # rebuilt above, so refresh).
    insp = inspect(bind)
    if "uq_organizations_subdomain" not in _existing_indexes(insp, "organizations"):
        op.create_index(
            "uq_organizations_subdomain",
            "organizations",
            ["subdomain"],
            unique=True,
            sqlite_where=sa.text("subdomain IS NOT NULL"),
            postgresql_where=sa.text("subdomain IS NOT NULL"),
        )

    # ---------------------------------------------------------------------
    # 2. players — organization_id (FK + index) + backfill (idempotent)
    # ---------------------------------------------------------------------
    insp = inspect(bind)
    if "organization_id" not in _existing_columns(insp, "players"):
        with op.batch_alter_table(
            "players", naming_convention=_NAMING_CONVENTION
        ) as bop:
            bop.add_column(
                sa.Column(
                    "organization_id",
                    sa.Integer(),
                    sa.ForeignKey(
                        "organizations.id",
                        ondelete="SET NULL",
                        name="fk_players_organization_id_organizations",
                    ),
                    nullable=True,
                )
            )

    insp = inspect(bind)
    if "idx_players_org" not in _existing_indexes(insp, "players"):
        op.create_index("idx_players_org", "players", ["organization_id"])

    # Backfill private-coach players (team_profile.organization_id IS NULL)
    # stay NULL. Org-team players inherit their team's organization_id.
    # Idempotent: re-running just re-applies the same value.
    op.execute(
        """
        UPDATE players
        SET organization_id = (
            SELECT t.organization_id
            FROM team_profile t
            WHERE t.id = players.team_id
        )
        WHERE players.team_id IS NOT NULL
          AND players.organization_id IS NULL
        """
    )

    # ---------------------------------------------------------------------
    # 3. player_contacts — new table (idempotent)
    # ---------------------------------------------------------------------
    insp = inspect(bind)
    if not _table_exists(insp, "player_contacts"):
        op.create_table(
            "player_contacts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "player_id",
                sa.Integer(),
                sa.ForeignKey("players.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("parent_name", sa.Text(), nullable=True),
            sa.Column("parent_email", sa.Text(), nullable=True),
            # EncryptedText is a TypeDecorator over Text — DDL writes Text.
            sa.Column("parent_phone_enc", sa.Text(), nullable=True),
            sa.Column("national_id_enc", sa.Text(), nullable=True),
            sa.Column("medical_notes_enc", sa.Text(), nullable=True),
            sa.Column("address_enc", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
            ),
            sa.UniqueConstraint("player_id", name="uq_player_contacts_player"),
        )
        op.create_index(
            "idx_player_contacts_org", "player_contacts", ["organization_id"]
        )

    # ---------------------------------------------------------------------
    # 4. Postgres RLS — no-op on SQLite. Idempotent: ALTER ... ENABLE is OK
    #    to re-run; CREATE POLICY uses IF NOT EXISTS-equivalent guard.
    # ---------------------------------------------------------------------
    if is_pg:
        for tbl in ("players", "player_contacts"):
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

    # 4. Drop RLS first (Postgres only)
    if is_pg:
        for tbl in ("player_contacts", "players"):
            op.execute(f"DROP POLICY IF EXISTS org_isolation ON {tbl}")
            op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")

    # 3. Drop player_contacts
    if _table_exists(insp, "player_contacts"):
        op.drop_index("idx_player_contacts_org", table_name="player_contacts")
        op.drop_table("player_contacts")

    # 2. Drop players.organization_id
    insp = inspect(bind)
    if "idx_players_org" in _existing_indexes(insp, "players"):
        op.drop_index("idx_players_org", table_name="players")
    insp = inspect(bind)
    if "organization_id" in _existing_columns(insp, "players"):
        with op.batch_alter_table(
            "players", naming_convention=_NAMING_CONVENTION
        ) as bop:
            bop.drop_column("organization_id")

    # 1. Drop organizations columns + subdomain index
    insp = inspect(bind)
    if "uq_organizations_subdomain" in _existing_indexes(insp, "organizations"):
        op.drop_index("uq_organizations_subdomain", table_name="organizations")
    insp = inspect(bind)
    existing_org = _existing_columns(insp, "organizations")
    cols_to_drop = [n for (n, _t) in _ORG_NEW_COLUMNS if n in existing_org]
    if cols_to_drop:
        with op.batch_alter_table(
            "organizations", naming_convention=_NAMING_CONVENTION
        ) as bop:
            for col in cols_to_drop:
                bop.drop_column(col)
