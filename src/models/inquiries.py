"""Sales inquiries model.

`sales_inquiries` captures the contact form submitted from
`/contact-sales` (academy / enterprise plans). user_id is nullable for
unauthenticated submissions.

Origin: `backend/api/admin.py` (inline CREATE TABLE in v1.0-flask).
First-class model in the new repo per Phase 1 decision (see MIGRATION_TODO).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class SalesInquiry(Base):
    __tablename__ = "sales_inquiries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    organization: Mapped[str | None] = mapped_column(Text, nullable=True)
    country: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    num_coaches: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    num_teams: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    current_tools: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    status: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="new")
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["SalesInquiry"]
