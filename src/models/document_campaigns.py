"""DocumentCampaign model — Phase 2.1.

A single bulk-send of a DocumentTemplate to N recipients (resolved at
send time from `recipient_filter`). Created in DRAFT, transitioned via
the campaign service.

Status state machine: DRAFT → QUEUED → SENDING → COMPLETED  (or → CANCELLED)

Lifecycle stats (total_recipients, total_signed, …) are denormalized
here so the dashboard never has to count rows in document_deliveries.

Part A note: the model exists so 2.3's signing flow has a parent FK to
hang off, but the actual bulk-send worker lives in Part B (Sub-Phase 2.4).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base, JSONText

if TYPE_CHECKING:
    from src.models.document_templates import DocumentTemplate
    from src.models.organizations import Organization


class DocumentCampaign(Base):
    __tablename__ = "document_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    template_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("document_templates.id", ondelete="CASCADE"), nullable=False
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)

    # Resolution rules — shape:
    # {"type": "all"|"region"|"branch"|"team"|"specific_players",
    #  "region_id": int|null, "branch_id": int|null,
    #  "team_ids": [int, ...], "player_ids": [int, ...]}
    recipient_filter: Mapped[dict] = mapped_column(JSONText, nullable=False)
    # Email / SMS preference (priority order). E.g. ["email", "sms"].
    delivery_channels: Mapped[list[str]] = mapped_column(JSONText, nullable=False)

    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # DRAFT | QUEUED | SENDING | COMPLETED | CANCELLED
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="DRAFT")

    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reminder_after_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True, server_default="7"
    )

    # Denormalized counters — updated by the send / sign workers.
    total_recipients: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    total_signed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    total_opened: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    total_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship(
        "Organization", lazy="raise", foreign_keys=[organization_id]
    )
    template: Mapped[DocumentTemplate] = relationship(
        "DocumentTemplate", lazy="raise", foreign_keys=[template_id]
    )

    __table_args__ = (
        Index("idx_doc_campaigns_org", "organization_id"),
        Index("idx_doc_campaigns_status", "status"),
        Index("idx_doc_campaigns_template", "template_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DocumentCampaign id={self.id} status={self.status} org={self.organization_id}>"


__all__ = ["DocumentCampaign"]
