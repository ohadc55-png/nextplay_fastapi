"""UserOrganization model — many-to-many pivot for user ↔ organization with role + scope.

A user can hold multiple roles in one or several orgs. Roles in active hierarchy:
`org_admin`, `program_manager`, `region_manager`, `coach`, `viewer`.
(`branch_manager` exists for backwards compat with Phase 0 but isn't part of
the Sha'ar Shivyon hierarchy.)

Scope columns:
- `program_id` set only for `program_manager`.
- `region_id` set only for `region_manager` (or coach pinned to a region).
- `branch_id` legacy — kept for Phase 0 rows; not assigned in Sha'ar Shivyon.

App-layer (NOT FK-enforced) constraints in UserOrganizationsRepository.create:
- `program_manager` requires `program_id` and forbids `region_id`/`branch_id`.
- `region_manager` requires `region_id` and forbids `branch_id`.
- `branch_manager` requires `branch_id` (legacy path).
- `region.organization_id`, `program.organization_id`, and
  `branch.organization_id` must equal this row's `organization_id`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base, JSONText

if TYPE_CHECKING:
    from src.models.branches import Branch
    from src.models.organizations import Organization
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.users import User


class UserOrganization(Base):
    __tablename__ = "user_organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    program_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("programs.id", ondelete="SET NULL"), nullable=True
    )
    region_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True
    )
    branch_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("branches.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    invited_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    invited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attributes_json: Mapped[dict | None] = mapped_column(JSONText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(
        "User", back_populates="organizations", lazy="raise", foreign_keys=[user_id]
    )
    organization: Mapped[Organization] = relationship(
        "Organization", back_populates="members", lazy="raise"
    )
    program: Mapped[Program | None] = relationship("Program", lazy="raise", foreign_keys=[program_id])
    region: Mapped[Region | None] = relationship("Region", lazy="raise", foreign_keys=[region_id])
    branch: Mapped[Branch | None] = relationship("Branch", lazy="raise", foreign_keys=[branch_id])
    inviter: Mapped[User | None] = relationship(
        "User", lazy="raise", foreign_keys=[invited_by]
    )

    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", "role", name="uq_user_org_role"),
        Index("idx_user_org_user", "user_id"),
        Index("idx_user_org_org", "organization_id"),
        Index("idx_user_org_org_role", "organization_id", "role"),
        Index("idx_user_org_program", "program_id"),
        Index("idx_user_org_branch", "branch_id"),
        Index("idx_user_org_region", "region_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<UserOrganization id={self.id} user_id={self.user_id} "
            f"org_id={self.organization_id} role={self.role!r}>"
        )


__all__ = ["UserOrganization"]
