"""OTPAttempt model — Phase 2.1.

Short-lived rate-limited records of OTP issuance + verification attempts
for the public signing flow. Codes are stored as SHA-256 hex (64 chars,
O(1) compare) — never plaintext.

`organization_id` is denormalized so RLS works uniformly with the other
Phase 2 tables (see plan §5 "Decision"). The org is always known at OTP
issue time — we look it up from the DocumentDelivery.

`delivery_token` is the same hex string stored on document_deliveries —
joined logically, not via FK, because (a) the lookup is hot, and (b) we
don't want a hard FK that would prevent cleanup of stale OTPs after a
delivery is deleted.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class OTPAttempt(Base):
    __tablename__ = "otp_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    delivery_token: Mapped[str] = mapped_column(Text, nullable=False)  # 32 hex chars
    phone: Mapped[str] = mapped_column(Text, nullable=False)  # plaintext snapshot
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)  # sha256 hex

    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="3"
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_otp_attempts_token", "delivery_token"),
        Index("idx_otp_attempts_phone", "phone"),
        Index("idx_otp_attempts_created", "created_at"),
        Index("idx_otp_attempts_org", "organization_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<OTPAttempt id={self.id} attempts={self.attempts}/"
            f"{self.max_attempts} verified={self.verified_at is not None}>"
        )


__all__ = ["OTPAttempt"]
