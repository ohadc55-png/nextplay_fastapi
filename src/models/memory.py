"""Long-term memory + knowledge graph models.

The "memory subsystem" is what gives the agents continuity across sessions:

- `memories`: persistent facts the agents can recall. `team_id` is nullable —
  rows with `team_id IS NULL` are coach-personal (style, preference,
  philosophy) and apply across all the coach's teams. Rows with `team_id`
  set are team-specific (tactics, player insights). The 1536-dim embedding
  is stored as JSON-as-TEXT (`embedding_json`); cosine similarity is computed
  in Python — see MIGRATION_TODO for the future pgvector switch.
- `entities`: named things (player, opponent, play, strategy) tracked over
  time. UNIQUE on (user_id, team_id, entity_type, entity_name).
- `entity_observations`: append-only log of observations per entity.
- `session_summaries`: per-session compressed summaries used for context
  injection into the next session's system prompt.

Origin: `backend/migrations/add_memory_system.py` +
`add_memory_embeddings.py`.
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
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base, JSONText


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    team_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("team_profile.id"), nullable=True
    )  # NULL → coach-wide (shared across all teams)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    importance: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="5")
    access_count: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())
    superseded_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("memories.id"), nullable=True)
    active: Mapped[bool | None] = mapped_column(Boolean, nullable=True, server_default="true")
    # 1536-dim float vector serialized as JSON array text (text-embedding-3-small)
    embedding_json: Mapped[list | None] = mapped_column(JSONText, nullable=True)

    __table_args__ = (
        Index("idx_memories_user_team", "user_id", "team_id", "active"),
        Index("idx_memories_category", "user_id", "category", "active"),
        Index("idx_memories_user_active", "user_id", "active"),
    )


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_name: Mapped[str] = mapped_column(Text, nullable=False)
    attributes_json: Mapped[dict | None] = mapped_column(JSONText, nullable=True, server_default="'{}'")
    last_mentioned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mention_count: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="1")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "user_id", "team_id", "entity_type", "entity_name", name="uq_entities_unique"
        ),
        Index("idx_entities_user_team", "user_id", "team_id"),
    )


class EntityObservation(Base):
    __tablename__ = "entity_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=False)
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    source_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_entity_obs_entity", "entity_id"),
    )


class SessionSummary(Base):
    __tablename__ = "session_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    topics_json: Mapped[list | None] = mapped_column(JSONText, nullable=True, server_default="'[]'")
    agents_used_json: Mapped[list | None] = mapped_column(JSONText, nullable=True, server_default="'[]'")
    message_count: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_session_summaries_user", "user_id", "team_id"),
    )


__all__ = ["Memory", "Entity", "EntityObservation", "SessionSummary"]
