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
    # Phase 3 (active hierarchy). Teams under Sha'ar Shivyon are scoped via
    # region_id; branch_id stays for back-compat with the Phase 0 branches API.
    region_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True
    )
    # Phase 12 — program is independent of region. Until this column existed,
    # a team's program had to be inferred from Region.program_id, which made
    # it impossible for one region (e.g. "מחוז מרכז") to host teams from
    # multiple programs simultaneously. Direct FK decouples the two axes.
    program_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("programs.id", ondelete="SET NULL"), nullable=True
    )

    # Phase 15 — Coach Calendar. Lazily assigned from an 8-color palette
    # (see src/services/team_colors.py) the first time the team appears on
    # the calendar; chip color in the month grid. Manual override possible
    # via team_setup if a coach wants a specific hue.
    color_hex: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 15 — iCal feed token. Coaches mint one per team; the URL acts
    # as the credential (token IS the password) so we never check JWT on
    # the feed endpoint. Rotatable; rotation invalidates the old URL.
    ical_token: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)

    # Phase 15 — public-share token. Same model as ical_token but powers
    # /calendar/share/<token> — a read-only HTML page showing current +
    # next month, no NEXTPLAY signup CTA. For sharing with parents/players.
    share_token: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)

    owner: Mapped[User | None] = relationship(
        "User", back_populates="teams", lazy="raise", foreign_keys=[user_id]
    )

    __table_args__ = (
        Index("idx_team_profile_org", "organization_id"),
        Index("idx_team_profile_branch", "branch_id"),
        Index("idx_team_profile_region", "region_id"),
        Index("idx_team_profile_program", "program_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TeamProfile id={self.id} name={self.team_name!r} user_id={self.user_id}>"


__all__ = ["TeamProfile"]
