"""Email infrastructure models.

- `email_log`: audit trail of every outbound email (status: pending / sent /
  bounced / complained / failed). user_id nullable for system emails.
- `mailing_lists`: admin-defined segments for broadcast emails. UNIQUE name.
- `mailing_list_members`: M-M between lists and users. Composite PK.

Origin: `backend/migrations/add_email_infrastructure.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class EmailLog(Base):
    __tablename__ = "email_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    to_email: Mapped[str] = mapped_column(Text, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # pending|sent|bounced|complained|failed
    provider_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_email_log_user_template", "user_id", "template", "sent_at"),
        Index("idx_email_log_status_date", "status", "sent_at"),
    )


class MailingList(Base):
    __tablename__ = "mailing_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())


class MailingListMember(Base):
    """Composite-PK M-M join. No surrogate `id` — matches v1.0-flask schema."""

    __tablename__ = "mailing_list_members"

    list_id: Mapped[int] = mapped_column(Integer, ForeignKey("mailing_lists.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_mailing_list_members_user", "user_id"),
    )


__all__ = ["EmailLog", "MailingList", "MailingListMember"]
