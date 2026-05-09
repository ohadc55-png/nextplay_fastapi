"""Pydantic API schema registry.

Each domain module exposes Create/Update/Response variants where the
corresponding endpoint exists. Internal-only entities (cache rows,
audit logs ingested by the system, entity-observation events) are not
schema'd because they are never returned via public APIs.
"""

from src.schemas.admin import (
    AdminTaskCommentCreate,
    AdminTaskCommentResponse,
    AdminTaskCreate,
    AdminTaskResponse,
    AdminTaskSubtaskCreate,
    AdminTaskSubtaskResponse,
    AdminTaskSubtaskUpdate,
    AdminTaskUpdate,
)
from src.schemas.analytics import OnboardingEventCreate, PageViewCreate
from src.schemas.auth import (
    AuditLogResponse,
    ChangePasswordRequest,
    DeleteAccountRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TokenPair,
    VerifyEmailRequest,
)
from src.schemas.chat import (
    ChatChunk,
    ChatRequest,
    ConversationMessage,
    OpeningMessageRequest,
    SessionSummaryResponse,
)
from src.schemas.clubs import (
    ClubInfoResponse,
    ClubInviteRequest,
    ClubResponse,
    InviteCodeCreate,
    InviteCodeResponse,
    RedeemCodeRequest,
)
from src.schemas.coach import (
    CoachPreferenceResponse,
    CoachPreferenceUpdate,
    FeedbackCreate,
    FeedbackResponse,
)
from src.schemas.common import ORMModel, PaginatedResponse, StatusResponse
from src.schemas.email import (
    EmailBroadcastRequest,
    EmailPreviewRequest,
    EmailTestSendRequest,
    MailingListCreate,
    MailingListMemberAdd,
)
from src.schemas.inquiries import SalesInquiryCreate, SalesInquiryResponse
from src.schemas.notebook import (
    NotebookAttendanceCreate,
    NotebookAttendanceResponse,
    NotebookEntryCreate,
    NotebookEntryResponse,
    NotebookEntryUpdate,
    NotebookFormatForSaveRequest,
)
from src.schemas.players import (
    PlayerCreate,
    PlayerGameStatCreate,
    PlayerGameStatResponse,
    PlayerMetricsResponse,
    PlayerMetricsUpdate,
    PlayerResponse,
    PlayerUpdate,
)
from src.schemas.plays import (
    PlayCreate,
    PlayResponse,
    PlayShareCreate,
    PlayShareResponse,
    PlayUpdate,
)
from src.schemas.push import (
    PushKeysSchema,
    PushPreferencesUpdate,
    PushSubscribeRequest,
    PushTestRequest,
    VapidKeyResponse,
)
from src.schemas.scouting import (
    ClipPlaylistCreate,
    ClipPlaylistResponse,
    ClipShareCreate,
    ClipShareResponse,
    CompileCardCreate,
    CompileCardResponse,
    PlaylistItemCreate,
    S3CompleteMultipartRequest,
    S3PresignUploadRequest,
    S3PresignUploadResponse,
    ScoutingPlayerCreate,
    ScoutingPlayerResponse,
    ScoutingVideoCreate,
    ScoutingVideoResponse,
    ScoutingVideoUpdate,
    VideoAnnotationCreate,
    VideoAnnotationResponse,
    VideoClipCreate,
    VideoClipResponse,
)
from src.schemas.teams import TeamCreate, TeamResponse, TeamUpdate
from src.schemas.uploads import UploadResponse
from src.schemas.users import ClubMemberSummary, UserMeResponse, UserResponse, UserUpdate

__all__ = [
    # Common
    "ORMModel",
    "PaginatedResponse",
    "StatusResponse",
    # Auth
    "RegisterRequest",
    "LoginRequest",
    "TokenPair",
    "RefreshRequest",
    "ForgotPasswordRequest",
    "ResetPasswordRequest",
    "ChangePasswordRequest",
    "VerifyEmailRequest",
    "ResendVerificationRequest",
    "DeleteAccountRequest",
    "AuditLogResponse",
    # Users
    "UserResponse",
    "UserMeResponse",
    "UserUpdate",
    "ClubMemberSummary",
    # Clubs
    "ClubResponse",
    "ClubInfoResponse",
    "ClubInviteRequest",
    "InviteCodeCreate",
    "InviteCodeResponse",
    "RedeemCodeRequest",
    # Teams
    "TeamCreate",
    "TeamUpdate",
    "TeamResponse",
    # Players
    "PlayerCreate",
    "PlayerUpdate",
    "PlayerResponse",
    "PlayerMetricsUpdate",
    "PlayerMetricsResponse",
    "PlayerGameStatCreate",
    "PlayerGameStatResponse",
    # Coach
    "CoachPreferenceUpdate",
    "CoachPreferenceResponse",
    "FeedbackCreate",
    "FeedbackResponse",
    # Uploads
    "UploadResponse",
    # Chat
    "ChatRequest",
    "OpeningMessageRequest",
    "ChatChunk",
    "ConversationMessage",
    "SessionSummaryResponse",
    # Notebook
    "NotebookEntryCreate",
    "NotebookEntryUpdate",
    "NotebookEntryResponse",
    "NotebookAttendanceCreate",
    "NotebookAttendanceResponse",
    "NotebookFormatForSaveRequest",
    # Plays
    "PlayCreate",
    "PlayUpdate",
    "PlayResponse",
    "PlayShareCreate",
    "PlayShareResponse",
    # Scouting
    "ScoutingVideoCreate",
    "ScoutingVideoUpdate",
    "ScoutingVideoResponse",
    "S3PresignUploadRequest",
    "S3PresignUploadResponse",
    "S3CompleteMultipartRequest",
    "VideoClipCreate",
    "VideoClipResponse",
    "VideoAnnotationCreate",
    "VideoAnnotationResponse",
    "ClipPlaylistCreate",
    "ClipPlaylistResponse",
    "PlaylistItemCreate",
    "ClipShareCreate",
    "ClipShareResponse",
    "ScoutingPlayerCreate",
    "ScoutingPlayerResponse",
    "CompileCardCreate",
    "CompileCardResponse",
    # Admin
    "AdminTaskCreate",
    "AdminTaskUpdate",
    "AdminTaskResponse",
    "AdminTaskSubtaskCreate",
    "AdminTaskSubtaskUpdate",
    "AdminTaskSubtaskResponse",
    "AdminTaskCommentCreate",
    "AdminTaskCommentResponse",
    # Analytics
    "PageViewCreate",
    "OnboardingEventCreate",
    # Push
    "PushKeysSchema",
    "PushSubscribeRequest",
    "PushPreferencesUpdate",
    "PushTestRequest",
    "VapidKeyResponse",
    # Inquiries
    "SalesInquiryCreate",
    "SalesInquiryResponse",
    # Email broadcast
    "EmailPreviewRequest",
    "EmailTestSendRequest",
    "EmailBroadcastRequest",
    "MailingListCreate",
    "MailingListMemberAdd",
]
