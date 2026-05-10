"""Repository registry.

As concrete repositories land in subsequent batches they get re-exported here
so callers can `from src.repositories import UsersRepo` without remembering the
file each one lives in.
"""

from src.repositories.admin_repo import (
    AdminTaskCommentsRepository,
    AdminTasksRepository,
    AdminTaskSubtasksRepository,
)
from src.repositories.analytics_repo import (
    ApiUsageLogsRepository,
    OnboardingEventsRepository,
    PageViewsRepository,
    ResearchUrlLogRepository,
)
from src.repositories.auth_repo import (
    AuditLogRepository,
    AuthTokenRepository,
    RefreshTokenRepository,
    SocialAccountRepository,
)
from src.repositories.base_repository import BaseRepository, TeamScopedRepository
from src.repositories.branches_repo import BranchesRepository
from src.repositories.cache_repo import IpGeoCacheRepository
from src.repositories.clubs_repo import ClubsRepository, InviteCodesRepository
from src.repositories.coach_repo import CoachPreferencesRepository, FeedbackRepository
from src.repositories.conversations_repo import ConversationsRepository
from src.repositories.email_repo import (
    EmailLogRepository,
    MailingListMembersRepository,
    MailingListsRepository,
)
from src.repositories.inquiries_repo import SalesInquiriesRepository
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
from src.repositories.org_audit_repo import OrgAuditRepository
from src.repositories.org_invites_repo import OrgInvitesRepository
from src.repositories.org_scoped_repository import OrgScopedRepository
from src.repositories.organizations_repo import OrganizationsRepository
from src.repositories.players_repo import (
    PlayerGameStatsRepository,
    PlayerMetricsRepository,
    PlayersRepository,
)
from src.repositories.plays_repo import PlaySharesRepository, PlaysRepository
from src.repositories.push_repo import PushLogRepository, PushSubscriptionsRepository
from src.repositories.regions_repo import RegionsRepository
from src.repositories.scouting_repo import (
    ClipPlaylistsRepository,
    ClipSharesRepository,
    CompileCardsRepository,
    PlaylistItemsRepository,
    ScoutingPlayersRepository,
    ScoutingVideosRepository,
    StorageQuotaRepository,
    VideoAnnotationsRepository,
    VideoClipsRepository,
)
from src.repositories.teams_repo import TeamsRepository
from src.repositories.uploads_repo import UploadsRepository
from src.repositories.user_organizations_repo import UserOrganizationsRepository
from src.repositories.users_repo import UsersRepository

__all__ = [
    # Base
    "BaseRepository",
    "TeamScopedRepository",
    "OrgScopedRepository",
    # Identity / auth / B2B
    "UsersRepository",
    "RefreshTokenRepository",
    "AuthTokenRepository",
    "SocialAccountRepository",
    "AuditLogRepository",
    "ClubsRepository",
    "InviteCodesRepository",
    # Multi-org Enterprise (Phase 0)
    "OrganizationsRepository",
    "RegionsRepository",
    "BranchesRepository",
    "UserOrganizationsRepository",
    "OrgInvitesRepository",
    "OrgAuditRepository",
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
    # Coach personalization
    "CoachPreferencesRepository",
    "FeedbackRepository",
    # Scouting + video room
    "ScoutingVideosRepository",
    "VideoClipsRepository",
    "VideoAnnotationsRepository",
    "ClipPlaylistsRepository",
    "PlaylistItemsRepository",
    "ClipSharesRepository",
    "StorageQuotaRepository",
    "ScoutingPlayersRepository",
    "CompileCardsRepository",
    # Email
    "EmailLogRepository",
    "MailingListsRepository",
    "MailingListMembersRepository",
    # Push
    "PushSubscriptionsRepository",
    "PushLogRepository",
    # Admin
    "AdminTasksRepository",
    "AdminTaskSubtasksRepository",
    "AdminTaskCommentsRepository",
    # Analytics
    "PageViewsRepository",
    "OnboardingEventsRepository",
    "ApiUsageLogsRepository",
    "ResearchUrlLogRepository",
    # Caches & misc
    "IpGeoCacheRepository",
    "SalesInquiriesRepository",
]
