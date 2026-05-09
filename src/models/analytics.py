"""Analytics + observability models.

- `page_views`: per-route navigation log; powers admin Activity heatmap.
- `onboarding_events`: first-use milestone tracker (UNIQUE per user/team/event).
- `api_usage_logs`: every OpenAI call logged for cost tracking.
- `research_url_log`: audit of every URL the Research Agent extracted from.

Origin: `backend/migrations/add_page_views.py`,
`add_onboarding_events.py`, `add_api_usage_logs.py` +
`fix_api_usage_logs_timestamp.py`, `add_research_url_log.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class PageView(Base):
    __tablename__ = "page_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    page_path: Mapped[str] = mapped_column(Text, nullable=False)
    page_section: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    entered_at: Mapped[str] = mapped_column(Text, nullable=False)
    exited_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("idx_pv_user", "user_id"),
        Index("idx_pv_session", "session_id"),
        Index("idx_pv_section", "page_section"),
        Index("idx_pv_date", "created_at"),
    )


class OnboardingEvent(Base):
    __tablename__ = "onboarding_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "team_id", "event", name="uq_onb_user_team_event"),
        Index("idx_onb_user_team", "user_id", "team_id"),
    )


class ApiUsageLog(Base):
    """Per-OpenAI-call cost tracking.

    `created_at` is TIMESTAMP on Postgres (after `fix_api_usage_logs_timestamp`
    migration); was originally TEXT. We type as `DateTime` to match the live
    Postgres state.
    """

    __tablename__ = "api_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)
    agent_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    cost_usd: Mapped[float | None] = mapped_column(Float(precision=24), nullable=True, server_default="0")  # REAL in prod
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="chat")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_api_usage_user", "user_id"),
        Index("idx_api_usage_date", "created_at"),
        Index("idx_api_usage_model", "model"),
    )


class ResearchUrlLog(Base):
    __tablename__ = "research_url_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    findings_count: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_research_url_log_domain", "domain"),
        Index("idx_research_url_log_tier", "tier"),
        Index("idx_research_url_log_used_at", "used_at"),
    )


__all__ = ["PageView", "OnboardingEvent", "ApiUsageLog", "ResearchUrlLog"]
