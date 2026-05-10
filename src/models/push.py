"""Web Push notification models.

- `push_subscriptions`: one row per (user, browser/device). UNIQUE on
  endpoint dedupes resubscriptions.
- `push_log`: audit trail of attempts (sent / failed / declined). The
  `reason` column captures *why* a push was declined (quiet_zone,
  daily_cap, no_subs, ...).

Origin: `backend/migrations/add_push_infrastructure.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_push_subs_user", "user_id"),
    )


class PushLog(Base):
    __tablename__ = "push_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    deep_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # sent|failed|declined
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_push_log_user_sent", "user_id", "sent_at"),
        Index("idx_push_log_status", "status", "sent_at"),
    )


__all__ = ["PushLog", "PushSubscription"]
