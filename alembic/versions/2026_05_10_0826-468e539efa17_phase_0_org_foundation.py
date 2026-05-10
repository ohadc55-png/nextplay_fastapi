"""phase_0_org_foundation

Adds the multi-organization Enterprise foundation:
- 6 new tables: organizations, regions, branches, user_organizations,
  org_audit_logs, org_invites
- 3 new columns on existing tables: team_profile.organization_id,
  team_profile.branch_id, users.active_organization_id
- 2 new indexes on team_profile
- Widens auth_tokens.user_id to nullable (org_invite tokens may reference
  emails not yet in `users`)

Existing data is preserved as-is — every existing user has
active_organization_id IS NULL, every existing team has
organization_id IS NULL (private coach mode).

Revision ID: 468e539efa17
Revises: 34b9481e6509
Create Date: 2026-05-10 08:26:45.658422
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from src.core.database import JSONText


revision: str = "468e539efa17"
down_revision: Union[str, Sequence[str], None] = "34b9481e6509"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) organizations — created first; FK to users.id resolves against existing baseline.
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("plan", sa.Text(), nullable=False, server_default="enterprise"),
        sa.Column("attributes_json", JSONText(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_organizations_created_by", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("idx_organizations_slug", "organizations", ["slug"])
    op.create_index("idx_organizations_status", "organizations", ["status"])
    op.create_index("idx_organizations_deleted_at", "organizations", ["deleted_at"])

    # 2) regions
    op.create_table(
        "regions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("attributes_json", JSONText(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_regions_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_regions_org_name"),
    )
    op.create_index("idx_regions_org", "regions", ["organization_id"])

    # 3) branches
    op.create_table(
        "branches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("region_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("attributes_json", JSONText(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_branches_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["region_id"], ["regions.id"], name="fk_branches_region", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_branches_org_name"),
    )
    op.create_index("idx_branches_org", "branches", ["organization_id"])
    op.create_index("idx_branches_region", "branches", ["region_id"])

    # 4) user_organizations — pivot
    op.create_table(
        "user_organizations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("region_id", sa.Integer(), nullable=True),
        sa.Column("branch_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("invited_by", sa.Integer(), nullable=True),
        sa.Column("invited_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("attributes_json", JSONText(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_org_user", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_user_org_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["region_id"], ["regions.id"], name="fk_user_org_region", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], name="fk_user_org_branch", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"], ["users.id"], name="fk_user_org_invited_by", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "organization_id", "role", name="uq_user_org_role"),
    )
    op.create_index("idx_user_org_user", "user_organizations", ["user_id"])
    op.create_index("idx_user_org_org", "user_organizations", ["organization_id"])
    op.create_index("idx_user_org_org_role", "user_organizations", ["organization_id", "role"])
    op.create_index("idx_user_org_branch", "user_organizations", ["branch_id"])
    op.create_index("idx_user_org_region", "user_organizations", ["region_id"])

    # 5) org_audit_logs — append-only
    op.create_table(
        "org_audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_email", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True, server_default=""),
        sa.Column("attributes_json", JSONText(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_org_audit_org", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name="fk_org_audit_actor", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_org_audit_org_created", "org_audit_logs", ["organization_id", "created_at"]
    )
    op.create_index("idx_org_audit_actor", "org_audit_logs", ["actor_user_id"])
    op.create_index("idx_org_audit_action", "org_audit_logs", ["action"])

    # 6) org_invites — token row references auth_tokens.id
    op.create_table(
        "org_invites",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("region_id", sa.Integer(), nullable=True),
        sa.Column("branch_id", sa.Integer(), nullable=True),
        sa.Column("auth_token_id", sa.Integer(), nullable=False),
        sa.Column("invited_by", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attributes_json", JSONText(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_org_invites_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["region_id"], ["regions.id"], name="fk_org_invites_region", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["branches.id"], name="fk_org_invites_branch", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["auth_token_id"],
            ["auth_tokens.id"],
            name="fk_org_invites_auth_token",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"], ["users.id"], name="fk_org_invites_invited_by", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("auth_token_id", name="uq_org_invites_auth_token"),
    )
    op.create_index("idx_org_invites_org", "org_invites", ["organization_id"])
    op.create_index("idx_org_invites_email", "org_invites", ["email"])
    op.create_index("idx_org_invites_status", "org_invites", ["status"])

    # 7) Add team_profile.organization_id, branch_id + indexes.
    # SQLite does not support ALTER TABLE ADD CONSTRAINT FK on an existing table,
    # so the FK is added via op.create_foreign_key only on Postgres. SQLite tests
    # use Base.metadata.create_all() which honors the model-level FK declarations,
    # so test isolation is unaffected.
    op.add_column("team_profile", sa.Column("organization_id", sa.Integer(), nullable=True))
    op.add_column("team_profile", sa.Column("branch_id", sa.Integer(), nullable=True))
    op.create_index("idx_team_profile_org", "team_profile", ["organization_id"])
    op.create_index("idx_team_profile_branch", "team_profile", ["branch_id"])

    # 8) Add users.active_organization_id (nullable, NULL = private coach).
    op.add_column("users", sa.Column("active_organization_id", sa.Integer(), nullable=True))

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_team_profile_org",
            "team_profile",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_team_profile_branch",
            "team_profile",
            "branches",
            ["branch_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_users_active_org",
            "users",
            "organizations",
            ["active_organization_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 9) Widen auth_tokens.user_id to nullable (org_invite tokens may have no user yet).
    # Batch_alter is required on SQLite for ALTER COLUMN; auth_tokens has no quirky
    # defaults that would break the table rebuild.
    with op.batch_alter_table("auth_tokens") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # Reverse order. Use batch_alter only for the auth_tokens nullability change
    # (the only true ALTER COLUMN); column drops use direct DROP COLUMN
    # (supported on modern SQLite and on Postgres).
    with op.batch_alter_table("auth_tokens") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint("fk_users_active_org", "users", type_="foreignkey")
        op.drop_constraint("fk_team_profile_branch", "team_profile", type_="foreignkey")
        op.drop_constraint("fk_team_profile_org", "team_profile", type_="foreignkey")

    op.drop_column("users", "active_organization_id")

    op.drop_index("idx_team_profile_branch", table_name="team_profile")
    op.drop_index("idx_team_profile_org", table_name="team_profile")
    op.drop_column("team_profile", "branch_id")
    op.drop_column("team_profile", "organization_id")

    op.drop_index("idx_org_invites_status", table_name="org_invites")
    op.drop_index("idx_org_invites_email", table_name="org_invites")
    op.drop_index("idx_org_invites_org", table_name="org_invites")
    op.drop_table("org_invites")

    op.drop_index("idx_org_audit_action", table_name="org_audit_logs")
    op.drop_index("idx_org_audit_actor", table_name="org_audit_logs")
    op.drop_index("idx_org_audit_org_created", table_name="org_audit_logs")
    op.drop_table("org_audit_logs")

    op.drop_index("idx_user_org_region", table_name="user_organizations")
    op.drop_index("idx_user_org_branch", table_name="user_organizations")
    op.drop_index("idx_user_org_org_role", table_name="user_organizations")
    op.drop_index("idx_user_org_org", table_name="user_organizations")
    op.drop_index("idx_user_org_user", table_name="user_organizations")
    op.drop_table("user_organizations")

    op.drop_index("idx_branches_region", table_name="branches")
    op.drop_index("idx_branches_org", table_name="branches")
    op.drop_table("branches")

    op.drop_index("idx_regions_org", table_name="regions")
    op.drop_table("regions")

    op.drop_index("idx_organizations_deleted_at", table_name="organizations")
    op.drop_index("idx_organizations_status", table_name="organizations")
    op.drop_index("idx_organizations_slug", table_name="organizations")
    op.drop_table("organizations")
