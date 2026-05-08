"""Conversation model — single message in a chat session.

One row per message (user or assistant). `session_id` groups messages into a
conversation. Tenant-scoped via user_id + team_id; queries on a session must
also pass at least one of those for isolation.

Origin: `backend/db/__init__.py` `init_db()` + `add_user_id_columns` +
`add_team_id_columns` + `add_performance_indexes`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)  # UUID string
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)  # user | assistant | <agent_name>
    content: Mapped[str] = mapped_column(Text, nullable=False)
    agent_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_conversations_user_id", "user_id"),
        Index("idx_conversations_team_id", "team_id"),
        Index("idx_conversations_session", "session_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Conversation id={self.id} role={self.role!r} session={self.session_id[:8]}...>"


__all__ = ["Conversation"]
