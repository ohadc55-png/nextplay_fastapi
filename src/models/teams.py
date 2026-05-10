"""TeamProfile model — the "team" entity (one coach can own many).

Every coach-scoped data row in the system carries a `team_id` FK to this table
plus a `user_id` FK to `users`. Multi-tenancy isolation is enforced at the
repository layer via at-least-one-of(user_id, team_id) gating; the FKs are
declared but not used to look up the active team — see `users.active_team_id`
for that.

Origin: `backend/db/__init__.py` `init_db()` + `add_user_id_columns` +
`add_storage_limit`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base

if TYPE_CHECKING:
    from src.models.users import User


class TeamProfile(Base):
    __tablename__ = "team_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    team_name: Mapped[str] = mapped_column(Text, nullable=False)
    league: Mapped[str | None] = mapped_column(Text, nullable=True)
    division: Mapped[str | None] = mapped_column(Text, nullable=True)
    play_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    weaknesses: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())
    extra_storage_gb: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")

    # Multi-org Enterprise (Phase 0). NULL = private coach team.
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    branch_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("branches.id", ondelete="SET NULL"), nullable=True
    )

    owner: Mapped[User | None] = relationship(
        "User", back_populates="teams", lazy="raise", foreign_keys=[user_id]
    )

    __table_args__ = (
        Index("idx_team_profile_org", "organization_id"),
        Index("idx_team_profile_branch", "branch_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TeamProfile id={self.id} name={self.team_name!r} user_id={self.user_id}>"


__all__ = ["TeamProfile"]
