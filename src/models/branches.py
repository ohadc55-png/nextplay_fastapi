"""Branch model — physical site under an Organization (optionally under a Region).

`organization_id` is denormalized (also reachable via region) so RLS policies
and indexed lookups stay fast. App-layer rule: when `region_id` is set, that
region's `organization_id` MUST match this row's `organization_id`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base, JSONText

if TYPE_CHECKING:
    from src.models.organizations import Organization
    from src.models.regions import Region


class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    region_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    attributes_json: Mapped[dict | None] = mapped_column(JSONText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship(
        "Organization", back_populates="branches", lazy="raise"
    )
    region: Mapped[Region | None] = relationship(
        "Region", back_populates="branches", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_branches_org_name"),
        Index("idx_branches_org", "organization_id"),
        Index("idx_branches_region", "region_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Branch id={self.id} org_id={self.organization_id} name={self.name!r}>"


__all__ = ["Branch"]
