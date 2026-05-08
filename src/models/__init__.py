"""Aggregated model registry.

Importing `src.models` registers every ORM class with `Base.metadata`.
Alembic's env.py imports `from src.models import *` so autogenerate sees them.

Add new models to the imports below as they land in subsequent batches.
"""

from src.models.admin import AdminTask, AdminTaskComment, AdminTaskSubtask
from src.models.analytics import ApiUsageLog, OnboardingEvent, PageView, ResearchUrlLog
from src.models.auth import AuditLog, AuthToken, RefreshToken, SocialAccount
from src.models.cache import IpGeoCache
from src.models.clubs import Club, InviteCode
from src.models.coach import CoachPreference, Feedback
from src.models.conversations import Conversation
from src.models.email import EmailLog, MailingList, MailingListMember
from src.models.inquiries import SalesInquiry
from src.models.memory import Entity, EntityObservation, Memory, SessionSummary
from src.models.notebook import NotebookAttendance, NotebookEntry, NotebookEntryPlayer
from src.models.players import Player, PlayerGameStat, PlayerMetric
from src.models.plays import Play, PlayShare
from src.models.push import PushLog, PushSubscription
from src.models.scouting import (
    ClipPlaylist,
    ClipShare,
    CompileCard,
    PlaylistItem,
    ScoutingPlayer,
    ScoutingVideo,
    StorageQuota,
    VideoAnnotation,
    VideoClip,
)
from src.models.teams import TeamProfile
from src.models.uploads import Upload
from src.models.users import User

__all__ = [
    # Identity / auth
    "User",
    "SocialAccount",
    "RefreshToken",
    "AuthToken",
    "AuditLog",
    # B2B / billing
    "Club",
    "InviteCode",
    # Teams & roster
    "TeamProfile",
    "Player",
    "PlayerMetric",
    "PlayerGameStat",
    # Coach personalization
    "CoachPreference",
    "Feedback",
    # Content
    "Upload",
    "Conversation",
    # Notebook
    "NotebookEntry",
    "NotebookAttendance",
    "NotebookEntryPlayer",
    # Plays
    "Play",
    "PlayShare",
    # Scouting / video room
    "ScoutingVideo",
    "VideoClip",
    "VideoAnnotation",
    "ClipPlaylist",
    "PlaylistItem",
    "ClipShare",
    "StorageQuota",
    "ScoutingPlayer",
    "CompileCard",
    # Memory / knowledge graph
    "Memory",
    "Entity",
    "EntityObservation",
    "SessionSummary",
    # Email
    "EmailLog",
    "MailingList",
    "MailingListMember",
    # Push notifications
    "PushSubscription",
    "PushLog",
    # Admin task tracker
    "AdminTask",
    "AdminTaskSubtask",
    "AdminTaskComment",
    # Analytics / observability
    "PageView",
    "OnboardingEvent",
    "ApiUsageLog",
    "ResearchUrlLog",
    # Caches & misc
    "IpGeoCache",
    "SalesInquiry",
]
