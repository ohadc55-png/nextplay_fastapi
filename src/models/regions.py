"""Region model — first-level subdivision under an Organization.

Optional: orgs with FLAT structure may have zero regions.
Used for region_manager scope on UserOrganization.
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


class Region(Base):
    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    program_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("programs.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    attributes_json: Mapped[dict | None] = mapped_column(JSONText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship(
        "Organization", back_populates="regions", lazy="raise"
    )
    program: Mapped[Program | None] = relationship(
        "Program", back_populates="regions", lazy="raise"
    )
    branches: Mapped[list[Branch]] = relationship(
        "Branch", back_populates="region", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_regions_org_name"),
        Index("idx_regions_org", "organization_id"),
        Index("idx_regions_program", "program_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Region id={self.id} org_id={self.organization_id} name={self.name!r}>"


__all__ = ["Region"]
