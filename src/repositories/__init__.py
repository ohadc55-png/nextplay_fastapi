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
from src.repositories.conversations_repo import ConversationsRepository
from src.repositories.memory_repo import (
    EntityObservationsRepository,
    EntityRepository,
    MemoryRepository,
    SessionSummariesRepository,
)
from src.repositories.notebook_repo import (
    NotebookAttendanceRepository,
    NotebookEntriesRepository,
    NotebookEntryPlayersRepository,
)
from src.repositories.players_repo import (
    PlayerGameStatsRepository,
    PlayerMetricsRepository,
    PlayersRepository,
)
from src.repositories.plays_repo import PlaySharesRepository, PlaysRepository
from src.repositories.teams_repo import TeamsRepository
from src.repositories.uploads_repo import UploadsRepository
from src.repositories.users_repo import UsersRepository

__all__ = [
    # Base
    "BaseRepository",
    "TeamScopedRepository",
    # Identity / auth / B2B
    "UsersRepository",
    "RefreshTokenRepository",
    "AuthTokenRepository",
    "SocialAccountRepository",
    "AuditLogRepository",
    "ClubsRepository",
    "InviteCodesRepository",
    # Tenant data
    "TeamsRepository",
    "PlayersRepository",
    "PlayerMetricsRepository",
    "PlayerGameStatsRepository",
    "UploadsRepository",
    "ConversationsRepository",
    "NotebookEntriesRepository",
    "NotebookAttendanceRepository",
    "NotebookEntryPlayersRepository",
    "PlaysRepository",
    "PlaySharesRepository",
    # Memory
    "MemoryRepository",
    "EntityRepository",
    "EntityObservationsRepository",
    "SessionSummariesRepository",
]
