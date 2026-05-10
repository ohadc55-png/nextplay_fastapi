"""Organization model — top-level tenant boundary for the Enterprise tier.

Created in Phase 0 to support multi-org customers (e.g., Sha'ar Shivyon).
Private coaches have no organization; their teams keep `organization_id IS NULL`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base, JSONText

if TYPE_CHECKING:
    from src.models.branches import Branch
    from src.models.regions import Region
    from src.models.user_organizations import UserOrganization


class Organization(Base):
    """Top-level tenant entity. Owns regions, branches, memberships."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default="enterprise")
    attributes_json: Mapped[dict | None] = mapped_column(JSONText, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    regions: Mapped[list[Region]] = relationship(
        "Region", back_populates="organization", lazy="raise", cascade="all, delete-orphan"
    )
    branches: Mapped[list[Branch]] = relationship(
        "Branch", back_populates="organization", lazy="raise", cascade="all, delete-orphan"
    )
    members: Mapped[list[UserOrganization]] = relationship(
        "UserOrganization", back_populates="organization", lazy="raise", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_organizations_slug", "slug"),
        Index("idx_organizations_status", "status"),
        Index("idx_organizations_deleted_at", "deleted_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Organization id={self.id} slug={self.slug!r}>"


__all__ = ["Organization"]
