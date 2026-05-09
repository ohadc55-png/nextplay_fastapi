"""Repositories for the auth-domain tables.

Four small repos in one file (they share the auth domain and never grow
beyond a handful of methods each):

- `RefreshTokenRepository` — issue / lookup-by-hash / revoke / cleanup.
- `AuthTokenRepository` — single-use tokens (verify_email, reset_password,
  change_email). Lookup-by-hash + mark_used + cleanup_expired.
- `SocialAccountRepository` — OAuth account linkage (Google/Facebook/Apple).
- `AuditLogRepository` — append-only audit trail; only `add()` is exposed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.auth import AuditLog, AuthToken, RefreshToken, SocialAccount
from src.repositories.base_repository import BaseRepository


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, RefreshToken)

    async def get_active_by_hash(self, token_hash: str) -> RefreshToken | None:
        """Look up a non-revoked refresh token by its hash."""
        stmt = select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def revoke(self, token_id: int) -> None:
        """Mark a single token revoked; idempotent (re-revoke is a no-op)."""
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.id == token_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def revoke_all_for_user(self, user_id: int) -> None:
        """Used on logout-all and on account-delete."""
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def cleanup_expired(self) -> int:
        """Hard-delete tokens past their expires_at. Returns rows hit."""
        stmt = delete(RefreshToken).where(RefreshToken.expires_at < func.now())
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0


class AuthTokenRepository(BaseRepository[AuthToken]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, AuthToken)

    async def get_unused_by_hash(self, token_hash: str, *, purpose: str) -> AuthToken | None:
        """Look up an UNused token of a specific purpose. Returning None
        covers all of: missing token, wrong purpose, already used, expired."""
        now = datetime.utcnow()
        stmt = select(AuthToken).where(
            AuthToken.token_hash == token_hash,
            AuthToken.purpose == purpose,
            AuthToken.used_at.is_(None),
            AuthToken.expires_at > now,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_used(self, token_id: int) -> None:
        """Idempotent — marking an already-used token does nothing."""
        stmt = (
            update(AuthToken)
            .where(AuthToken.id == token_id, AuthToken.used_at.is_(None))
            .values(used_at=func.now())
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def cleanup_expired(self) -> int:
        """Cron-friendly purge of tokens past their expiry."""
        stmt = delete(AuthToken).where(AuthToken.expires_at < func.now())
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0


class SocialAccountRepository(BaseRepository[SocialAccount]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, SocialAccount)

    async def get_by_provider_user(
        self, *, provider: str, provider_user_id: str
    ) -> SocialAccount | None:
        """Find a linked OAuth account by (provider, provider_user_id)."""
        stmt = select(SocialAccount).where(
            SocialAccount.provider == provider,
            SocialAccount.provider_user_id == provider_user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_user(self, user_id: int) -> list[SocialAccount]:
        stmt = select(SocialAccount).where(SocialAccount.user_id == user_id)
        return list((await self.session.execute(stmt)).scalars().all())


class AuditLogRepository(BaseRepository[AuditLog]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, AuditLog)

    async def add(
        self,
        *,
        user_id: int | None,
        action: str,
        ip_address: str | None = None,
        user_agent: str | None = "",
        details: str | None = "",
    ) -> AuditLog:
        """Append a new audit row. Errors here should NOT bubble up to the
        caller (a failed audit shouldn't fail a login) — wrap in try/except
        in the calling service if needed."""
        row = AuditLog(
            user_id=user_id,
            action=action,
            ip_address=ip_address,
            user_agent=user_agent or "",
            details=details or "",
        )
        return await self.create(row)


__all__ = [
    "RefreshTokenRepository",
    "AuthTokenRepository",
    "SocialAccountRepository",
    "AuditLogRepository",
]
