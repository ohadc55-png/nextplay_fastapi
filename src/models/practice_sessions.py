"""PracticeSession model — scheduled team practice.

The Sha'ar Shivyon ops view ("האימונים שמתקיימים היום") needs a per-day
list of practices scoped to the active org / program / region / coach.
Storing org/program/region denormalized alongside team_id keeps that
sidebar query a single indexed range scan instead of a 4-way join.

Coach-owned data (the team) is the source of truth — denormalized
columns are written from the team at INSERT and only updated when the
team's own scope changes. For private coaches with no org these stay
NULL, matching the convention used elsewhere in the schema.

Status values: 'scheduled' | 'in_progress' | 'completed' | 'cancelled'.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base, JSONText

if TYPE_CHECKING:
    from src.models.organizations import Organization
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.teams import TeamProfile
    from src.models.users import User


class PracticeSession(Base):
    __tablename__ = "practice_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("team_profile.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Denormalized scope axes (mirrored from team_profile + the team's region/program
    # at INSERT). Allow NULL so private-coach practices stay non-org.
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    program_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("programs.id", ondelete="SET NULL"), nullable=True
    )
    region_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True
    )

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="scheduled")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes_json: Mapped[dict | None] = mapped_column(JSONText, nullable=True)

    # Phase 15 — Coach Calendar. When a coach attaches a practice plan they
    # built with the training agent, this FK points at the NotebookEntry
    # (entry_type='practice_plan'). Day-detail panel shows the link.
    practice_plan_entry_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("notebook_entries.id", ondelete="SET NULL"), nullable=True
    )

    # Phase 15 — when a single occurrence in a recurring series gets edited
    # (Google-Calendar "this only" semantics), we write a child row pointing
    # at the original series anchor. NULL = row IS the anchor (or a
    # standalone non-recurring event). Lookup pattern: series anchor has
    # the canonical `series_id` in attributes_json; children share the same
    # series_id AND point back via parent_event_id.
    parent_event_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("practice_sessions.id", ondelete="CASCADE"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    team: Mapped[TeamProfile] = relationship(
        "TeamProfile", lazy="raise", foreign_keys=[team_id]
    )
    coach: Mapped[User | None] = relationship(
        "User", lazy="raise", foreign_keys=[user_id]
    )
    organization: Mapped[Organization | None] = relationship(
        "Organization", lazy="raise", foreign_keys=[organization_id]
    )
    program: Mapped[Program | None] = relationship(
        "Program", lazy="raise", foreign_keys=[program_id]
    )
    region: Mapped[Region | None] = relationship(
        "Region", lazy="raise", foreign_keys=[region_id]
    )

    __table_args__ = (
        Index("idx_practice_team", "team_id"),
        Index("idx_practice_user", "user_id"),
        Index("idx_practice_org_scheduled", "organization_id", "scheduled_at"),
        Index("idx_practice_program_scheduled", "program_id", "scheduled_at"),
        Index("idx_practice_region_scheduled", "region_id", "scheduled_at"),
        Index("idx_practice_scheduled", "scheduled_at"),
        Index("idx_practice_plan_entry", "practice_plan_entry_id"),
        Index("idx_practice_parent", "parent_event_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<PracticeSession id={self.id} team_id={self.team_id} "
            f"at={self.scheduled_at!r} status={self.status!r}>"
        )


__all__ = ["PracticeSession"]
