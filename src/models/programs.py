"""Program model — first-level subdivision under an Organization.

Sha'ar Shivyon (the first Enterprise customer) runs 6 distinct sports
programs (שער שיוויון, בועטות, סל טק, מלכת הסלים, שווים לניצחון,
חותרים להצלחה). Each program owns one or more regions; each region owns
many teams.

Hierarchy: Organization → Program → Region → Team → Coach + Players.

A program_manager membership row carries `program_id` to scope reads to
this slice; a region_manager row carries `region_id` (region's
`program_id` is the implicit upper bound).

Programs are optional. Orgs that don't run multiple programs simply
keep regions with `program_id IS NULL` (treated as the org's single
default program at the route layer).
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


class Program(Base):
    __tablename__ = "programs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes_json: Mapped[dict | None] = mapped_column(JSONText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship(
        "Organization", back_populates="programs", lazy="raise"
    )
    regions: Mapped[list[Region]] = relationship(
        "Region", back_populates="program", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_programs_org_name"),
        Index("idx_programs_org", "organization_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Program id={self.id} org_id={self.organization_id} name={self.name!r}>"


__all__ = ["Program"]
