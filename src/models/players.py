"""Player + PlayerMetric + PlayerGameStat models.

- `players`: roster row, tenant-scoped (user_id + team_id). `active=False`
  is the soft-delete pattern for benched players.
- `player_metrics`: 1:1 with `players` (UNIQUE on player_id). Stores
  `metrics_json` as JSON-as-TEXT (uses `JSONText` decorator).
- `player_game_stats`: per-player, per-game box score. UNIQUE on
  (player_id, game_date, opponent). Optional FK to `notebook_entries`
  (linked when generated from a Game Summary entry).

Origin: `backend/db/__init__.py` (players + player_metrics base),
`backend/notebook/__init__.py` (player_game_stats), and migrations
add_user_id_columns / add_team_id_columns / add_player_photo /
add_player_scout_fields.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base, JSONText


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)
    # Phase 1.1 — denormalized for fast org-scoped queries + RLS. NULL means
    # private-coach player (existing rows pre-Phase-1; backfill in migration).
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[str | None] = mapped_column(Text, nullable=True)
    height: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight: Mapped[str | None] = mapped_column(Text, nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    weaknesses: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    dominant_hand: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    active: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=True, server_default="true"
    )  # NULLABLE in prod

    # Photo + scouting (added by migrations)
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    scout_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    metrics: Mapped[PlayerMetric | None] = relationship(
        "PlayerMetric", back_populates="player", uselist=False, lazy="raise", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_players_user_id", "user_id"),
        Index("idx_players_team_id", "team_id"),
        Index("idx_players_org", "organization_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Player id={self.id} name={self.name!r} team_id={self.team_id}>"


class PlayerMetric(Base):
    __tablename__ = "player_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)
    metrics_json: Mapped[dict] = mapped_column(JSONText, nullable=False, server_default="'{}'")
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    player: Mapped[Player] = relationship("Player", back_populates="metrics", lazy="raise")

    __table_args__ = (
        Index("idx_player_metrics_user_id", "user_id"),
        Index("idx_player_metrics_team_id", "team_id"),
    )


class PlayerGameStat(Base):
    """Box score row for one player in one game.

    UNIQUE on (player_id, game_date, opponent) ensures we don't double-write
    when the same Game Summary is reprocessed.
    """

    __tablename__ = "player_game_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=False)
    player_id: Mapped[int] = mapped_column(Integer, nullable=False)  # no FK in Flask schema
    notebook_entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    game_date: Mapped[str] = mapped_column(Text, nullable=False)  # ISO date string
    opponent: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")

    # Box score
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    points: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    fgm: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    fga: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    three_pm: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    three_pa: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    ftm: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    fta: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    oreb: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    dreb: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    reb: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    ast: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    stl: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    blk: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    turnovers: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    pf: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    plus_minus: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")

    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("player_id", "game_date", "opponent", name="uq_pgs_player_game"),
        Index("idx_pgs_player", "player_id", "game_date"),
        Index("idx_pgs_team_game", "user_id", "team_id", "game_date"),
        Index("idx_pgs_entry", "notebook_entry_id"),
    )


__all__ = ["Player", "PlayerGameStat", "PlayerMetric"]
