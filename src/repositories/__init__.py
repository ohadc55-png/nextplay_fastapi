"""Repository registry.

As concrete repositories land in subsequent batches they get re-exported here
so callers can `from src.repositories import UsersRepo` without remembering the
file each one lives in.
"""

from src.repositories.auth_repo import (
    AuditLogRepository,
    AuthTokenRepository,
    RefreshTokenRepository,
    SocialAccountRepository,
)
from src.repositories.base_repository import BaseRepository, TeamScopedRepository
from src.repositories.clubs_repo import ClubsRepository, InviteCodesRepository
from src.repositories.users_repo import UsersRepository

__all__ = [
    # Base
    "BaseRepository",
    "TeamScopedRepository",
    # Identity
    "UsersRepository",
    # Auth
    "RefreshTokenRepository",
    "AuthTokenRepository",
    "SocialAccountRepository",
    "AuditLogRepository",
    # B2B
    "ClubsRepository",
    "InviteCodesRepository",
]
