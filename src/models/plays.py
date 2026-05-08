"""Plays + play_shares.

- `plays`: tenant-scoped saved plays (offense + defense). Two JSON-as-TEXT
  payloads — `players_json` (positions / IDs) and `actions_json` (sequence
  of moves). `ball_holder_id` is stored as TEXT (player ID can be a temp
  UUID or numeric depending on the playbook editor).
- `play_shares`: snapshot a play under a public `token` for read-only sharing.

Origin: `backend/plays/__init__.py`.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base, JSONText


class Play(Base):
    __tablename__ = "plays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    offense_template: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="empty")
    defense_template: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="none")
    players_json: Mapped[list | None] = mapped_column(JSONText, nullable=True, server_default="'[]'")
    actions_json: Mapped[list | None] = mapped_column(JSONText, nullable=True, server_default="'[]'")
    ball_holder_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class PlayShare(Base):
    __tablename__ = "play_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    play_json: Mapped[dict] = mapped_column(JSONText, nullable=False)  # snapshot of full play
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["Play", "PlayShare"]
