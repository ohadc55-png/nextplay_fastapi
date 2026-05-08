"""Coach-side preference + feedback models.

- `coach_preferences`: per-user UNIQUE row that shapes agent behavior
  (preferred language, detail level, focus / avoid topics, ...). Lazily
  created in the Flask app via `_ensure_coach_preferences_table()`.
- `feedback`: per agent message thumbs up/down + comment, used to build
  reinforcement context for the next prompt.

Origin: `backend/db/__init__.py` lazy-create blocks.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class CoachPreference(Base):
    __tablename__ = "coach_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, unique=True
    )
    preferred_language: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="en")
    detail_level: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="medium")
    focus_areas: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    avoid_topics: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    practice_duration: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="90")
    coaching_style: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    custom_notes: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_coach_prefs_user", "user_id"),
    )


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)
    agent_key: Mapped[str] = mapped_column(Text, nullable=False)
    message_content: Mapped[str] = mapped_column(Text, nullable=False)
    response_content: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # +1 / -1
    comment: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_feedback_user_agent", "user_id", "agent_key"),
    )


__all__ = ["CoachPreference", "Feedback"]
