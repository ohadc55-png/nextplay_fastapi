"""OTPAttempts repository — Phase 2.3.

Two hot methods:
- `count_recent(token, hours)`: rate-limit guard on OTP issuance (per-token,
  not per-IP — IP rate limiting lives in the middleware).
- `latest_unverified(token)`: when a parent submits a code, we match against
  the most recently-issued unverified OTP for their token.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.otp_attempts import OTPAttempt
from src.repositories.base_repository import BaseRepository


class OTPAttemptsRepository(BaseRepository[OTPAttempt]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, OTPAttempt)

    async def count_recent(self, delivery_token: str, *, hours: int = 1) -> int:
        """Return how many OTPs were issued for this token in the last N
        hours. Used to cap reissue frequency (plan §6.2 — 3 per hour)."""
        if not delivery_token:
            return 0
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours)
        stmt = select(func.count(OTPAttempt.id)).where(
            OTPAttempt.delivery_token == delivery_token,
            OTPAttempt.created_at >= cutoff,
        )
        return int((await self.session.execute(stmt)).scalar() or 0)

    async def latest_unverified(self, delivery_token: str) -> OTPAttempt | None:
        """Latest unverified OTP for a token. None if all verified or no
        attempts exist. Used by the verify endpoint to find which code
        the parent is trying to match."""
        if not delivery_token:
            return None
        stmt = (
            select(OTPAttempt)
            .where(
                OTPAttempt.delivery_token == delivery_token,
                OTPAttempt.verified_at.is_(None),
            )
            .order_by(OTPAttempt.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = ["OTPAttemptsRepository"]
