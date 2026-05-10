"""add_org_rls_postgres

Layer 3 of the multi-org tenancy defense: PostgreSQL Row Level Security.
This revision is a NO-OP on SQLite (dev + tests + CI alembic-smoke); it
only runs DDL when the connection's dialect is PostgreSQL.

Policies installed (one per table):
- organizations: USING (id = current_setting('app.current_org_id', true)::int)
- regions / branches / user_organizations / org_invites / org_audit_logs /
  team_profile: USING (organization_id = current_setting(...)::int)

Plus REVOKE UPDATE, DELETE on org_audit_logs from the connecting role
(audit rows are immutable; updating or deleting one means tampering).

The application sets `app.current_org_id` per-transaction in
[src/core/database.py:get_db](../../src/core/database.py) when the request
has an active org context. Sessions without context (e.g., admin queries
that need all orgs) bypass RLS via the session being run as a Postgres
superuser or via FORCE-disabled connections — for Phase 0 we don't add
that escape hatch, so System-Admin queries from the regular pool must
hit the read paths directly (which already filter explicitly).

Revision ID: 9a3f2b1c4e7d
Revises: 468e539efa17
Create Date: 2026-05-10 11:21:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "9a3f2b1c4e7d"
down_revision: Union[str, Sequence[str], None] = "468e539efa17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables filtered by `organization_id = current_setting(...)`
_ORG_FK_TABLES: tuple[str, ...] = (
    "regions",
    "branches",
    "user_organizations",
    "org_invites",
    "org_audit_logs",
    "team_profile",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # --- organizations: filter by id (the org IS the row).
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organizations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY org_isolation ON organizations "
        "USING (id = current_setting('app.current_org_id', true)::int)"
    )

    # --- everything else: filter by organization_id.
    for tbl in _ORG_FK_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY org_isolation ON {tbl} "
            "USING (organization_id = current_setting('app.current_org_id', true)::int)"
        )

    # --- audit_logs are immutable: revoke UPDATE / DELETE.
    # The grant target is the role the app connects as (current_user). This
    # mirrors the Flask app's connection model. If a deployment uses a
    # dedicated read-only role for analytics, the revoke does NOT affect it.
    op.execute("REVOKE UPDATE, DELETE ON org_audit_logs FROM CURRENT_USER")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("GRANT UPDATE, DELETE ON org_audit_logs TO CURRENT_USER")

    for tbl in _ORG_FK_TABLES:
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS org_isolation ON organizations")
    op.execute("ALTER TABLE organizations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organizations DISABLE ROW LEVEL SECURITY")
