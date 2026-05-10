"""Club + invite_codes models.

`clubs` is the B2B organization tier (academy, league, club). One coach belongs
to at most one club via `users.club_id`. `invite_codes` are admin-generated,
single-use codes that can grant a subscription plan and/or auto-link the
redeemer to a club.

Origin: `backend/migrations/add_club_support.py`,
`backend/migrations/add_subscription_columns.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base

if TYPE_CHECKING:
    from src.models.users import User


class Club(Base):
    __tablename__ = "clubs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    subscription_plan: Mapped[str] = mapped_column(Text, nullable=False, server_default="academy10")
    max_seats: Mapped[int] = mapped_column(Integer, nullable=False, server_default="10")
    pooled_storage_gb: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=func.now())
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=func.now())

    members: Mapped[list[User]] = relationship("User", back_populates="club", lazy="raise")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Club id={self.id} name={self.name!r}>"


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan: Mapped[str] = mapped_column(Text, nullable=False, server_default="pro")
    redeemed_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    redeemed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    club_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("clubs.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_invite_code", "code"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<InviteCode id={self.id} code={self.code!r}>"


__all__ = ["Club", "InviteCode"]
