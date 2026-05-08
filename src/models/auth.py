"""Auth-related tables: social_accounts, refresh_tokens, auth_tokens, audit_logs.

- `social_accounts`: OAuth/social login linkage (Google, GitHub, ...).
  `provider_data` is JSON-as-TEXT.
- `refresh_tokens`: hashed JWT refresh tokens with device_info; revocation soft.
- `auth_tokens`: single-use, hashed tokens for verify_email / reset_password /
  change_email. Cleaned by cron when expires_at < NOW().
- `audit_logs`: append-only login/security audit trail.

Origin: `backend/auth/__init__.py` (first three) and
`backend/migrations/add_email_infrastructure.py` (`auth_tokens`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base, JSONText


class SocialAccount(Base):
    __tablename__ = "social_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-as-TEXT (raw provider payload)
    provider_data: Mapped[dict | None] = mapped_column(JSONText, nullable=True, server_default="'{}'")
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_social_provider_user"),
        Index("idx_social_provider", "provider", "provider_user_id"),
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    device_info: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)  # ISO 8601 string
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=func.now())
    revoked_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_refresh_hash", "token_hash"),
        Index("idx_refresh_user", "user_id"),
    )


class AuthToken(Base):
    """Single-use hashed tokens for email verification, password reset, etc."""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)  # verify_email | reset_password | change_email | ...
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_auth_tokens_user_purpose", "user_id", "purpose"),
        Index("idx_auth_tokens_expires", "expires_at"),
    )


class AuditLog(Base):
    """Append-only log of auth/security events (login, logout, password change, ...)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    details: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_audit_user", "user_id"),
    )


__all__ = ["SocialAccount", "RefreshToken", "AuthToken", "AuditLog"]
