"""Coach Notebook models.

- `notebook_entries`: one row per notebook entry (practice plan, game summary,
  player note, period plan, attendance, free document). Two JSON-as-TEXT
  payloads: `content_json` (entry-type-specific structure) and `tags_json`.
- `notebook_attendance`: per-player attendance for a notebook entry of type
  attendance / practice_plan. Soft-FK by player_id (no DB constraint).
- `notebook_entry_players`: M-M join between entries and players. Replaces
  the legacy `notebook_entries.player_id` (kept for back-compat read).

Origin: `backend/notebook/__init__.py` + `add_notebook_entry_players.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base, JSONText


class NotebookEntry(Base):
    __tablename__ = "notebook_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=False)
    entry_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    entry_date: Mapped[str] = mapped_column(Text, nullable=False)  # ISO date YYYY-MM-DD
    content_json: Mapped[dict] = mapped_column(JSONText, nullable=False, server_default="'{}'")
    player_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # legacy single-player link
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual")
    tags_json: Mapped[list] = mapped_column(JSONText, nullable=False, server_default="'[]'")
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 15 — Coach Calendar. Reverse link from a notebook entry
    # (attendance / free_document / game_summary) back to the calendar
    # event it describes. SET NULL on event delete so notebook history
    # stays intact when an event is removed.
    practice_session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("practice_sessions.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("idx_nb_entries_team_type", "user_id", "team_id", "entry_type"),
        Index("idx_nb_entries_date", "team_id", "entry_date"),
        Index("idx_nb_entries_player", "player_id"),
        Index("idx_nb_entries_practice_session", "practice_session_id"),
    )


class NotebookAttendance(Base):
    __tablename__ = "notebook_attendance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notebook_entries.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(Integer, nullable=False)  # soft-FK
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="present")
    note: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")

    __table_args__ = (
        UniqueConstraint("entry_id", "player_id", name="uq_nb_attendance_entry_player"),
        Index("idx_nb_attendance_entry", "entry_id"),
        Index("idx_nb_attendance_player", "player_id"),
    )


class NotebookEntryPlayer(Base):
    """M-M join between notebook entries and players.

    Replaces the legacy `notebook_entries.player_id` single-link. Backfilled
    from that column at migration time.
    """

    __tablename__ = "notebook_entry_players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notebook_entries.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(Integer, nullable=False)  # soft-FK (Flask schema)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("entry_id", "player_id", name="uq_nb_entry_players_entry_player"),
        Index("idx_nb_entry_players_player", "player_id"),
    )


__all__ = ["NotebookAttendance", "NotebookEntry", "NotebookEntryPlayer"]
