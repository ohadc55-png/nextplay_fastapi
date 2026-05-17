"""Aggregated model registry.

Importing `src.models` registers every ORM class with `Base.metadata`.
Alembic's env.py imports `from src.models import *` so autogenerate sees them.

Add new models to the imports below as they land in subsequent batches.
"""

from src.models.admin import AdminTask, AdminTaskComment, AdminTaskSubtask
from src.models.analytics import ApiUsageLog, OnboardingEvent, PageView, ResearchUrlLog
from src.models.auth import AuditLog, AuthToken, RefreshToken, SocialAccount
from src.models.branches import Branch
from src.models.cache import IpGeoCache
from src.models.clubs import Club, InviteCode
from src.models.coach import CoachPreference, Feedback
from src.models.conversations import Conversation
from src.models.document_campaigns import DocumentCampaign
from src.models.document_deliveries import DocumentDelivery
from src.models.document_templates import DocumentTemplate
from src.models.email import EmailLog, MailingList, MailingListMember
from src.models.inquiries import SalesInquiry
from src.models.memory import Entity, EntityObservation, Memory, SessionSummary
from src.models.messages import Message, MessageDelivery
from src.models.notebook import NotebookAttendance, NotebookEntry, NotebookEntryPlayer
from src.models.org_audit import OrgAuditLog
from src.models.org_invites import OrgInvite
from src.models.organizations import Organization
from src.models.otp_attempts import OTPAttempt
from src.models.player_contacts import PlayerContact
from src.models.players import Player, PlayerGameStat, PlayerMetric
from src.models.plays import Play, PlayShare
from src.models.practice_sessions import PracticeSession
from src.models.programs import Program
from src.models.push import PushLog, PushSubscription
from src.models.regions import Region
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
from src.models.user_organizations import UserOrganization
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
    # Multi-org Enterprise (Phase 0 + active hierarchy)
    "Organization",
    "Program",
    "Region",
    "Branch",
    "UserOrganization",
    "OrgAuditLog",
    "OrgInvite",
    # Teams & roster
    "TeamProfile",
    "Player",
    "PlayerContact",
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
    # Practice scheduling
    "PracticeSession",
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
    # Phase 2.1 — Documents + Messaging
    "DocumentTemplate",
    "DocumentCampaign",
    "DocumentDelivery",
    "OTPAttempt",
    "Message",
    "MessageDelivery",
]
