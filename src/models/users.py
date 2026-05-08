"""User account model.

Maps the live `users` table — base columns from `backend/auth/__init__.py` plus
columns added by `add_user_id_columns`, `add_team_id_columns`,
`add_email_infrastructure`, `add_push_infrastructure`, `add_subscription_columns`,
`add_club_support`, `add_data_purge_at`.

Type choices match the live PostgreSQL schema exactly:
- `is_active` / `email_verified` / `is_club_admin` are stored as INTEGER (0/1)
  because the Flask init wrote them with `INTEGER DEFAULT 1`. Newer flags
  (`email_marketing`, `email_infra_signup`, `push_enabled`) were added as
  BOOLEAN in their respective migrations, so we preserve that distinction.
- Most timestamps live as TEXT (ISO 8601 strings written by `datetime.now().isoformat()`)
  except for `last_push_sent_at`, `last_seen_at`, `data_purge_at` which were added as TIMESTAMP.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base

if TYPE_CHECKING:
    from src.models.clubs import Club
    from src.models.teams import TeamProfile


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identity
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="coach")

    # Lifecycle (legacy INTEGER booleans — preserve)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    email_verified: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deleted_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=func.now())
    last_login_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Active team selector (multi-team support; from add_team_id_columns)
    active_team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Email infrastructure (from add_email_infrastructure)
    email_marketing: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    unsubscribe_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_infra_signup: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Push notifications (from add_push_infrastructure)
    push_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    push_quiet_start: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, server_default="22")
    push_quiet_end: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, server_default="7")
    last_push_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timezone: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="Asia/Jerusalem")

    # Subscription (from add_subscription_columns)
    subscription_plan: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="trial")
    trial_ends_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Club membership (from add_club_support)
    club_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("clubs.id"), nullable=True)
    is_club_admin: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")

    # Data purge (from add_data_purge_at)
    data_purge_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships — populated as their target models land in later batches.
    club: Mapped["Club | None"] = relationship("Club", back_populates="members", lazy="raise")
    teams: Mapped[list["TeamProfile"]] = relationship(
        "TeamProfile", back_populates="owner", lazy="raise", foreign_keys="TeamProfile.user_id"
    )

    __table_args__ = (
        Index("idx_users_email", "email"),
        Index("idx_users_deleted", "deleted_at"),
        Index("idx_users_club", "club_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email!r}>"


__all__ = ["User"]
