"""Message + MessageDelivery models — Phase 2.1.

Separate from documents — simple notifications/reminders to parents.
No signature, no OTP, no tokens. Just queue and send.

We put both models in this file (mirroring the players.py / scouting.py
pattern of housing related domain models together).

Part A note: the schema lands now so 2.5 can wire endpoints on top
without a follow-up migration. The actual send worker is Part B.
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
    from src.models.organizations import Organization
    from src.models.player_contacts import PlayerContact
    from src.models.players import Player


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    subject: Mapped[str] = mapped_column(Text, nullable=False)
    # Plain text for SMS, may include basic HTML for email.
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Same shape as DocumentCampaign.recipient_filter.
    recipient_filter: Mapped[dict] = mapped_column(JSONText, nullable=False)
    delivery_channels: Mapped[list[str]] = mapped_column(JSONText, nullable=False)

    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # DRAFT | SCHEDULED | SENDING | SENT | CANCELLED
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="DRAFT")

    total_recipients: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    total_delivered: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    total_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    sent_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship(
        "Organization", lazy="raise", foreign_keys=[organization_id]
    )

    __table_args__ = (
        Index("idx_messages_org", "organization_id"),
        Index("idx_messages_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Message id={self.id} status={self.status} org={self.organization_id}>"


class MessageDelivery(Base):
    """Per-recipient delivery row for a Message.

    Deviation from the Phase 2 master prompt: we use player_contact_id
    (FK → player_contacts.id) instead of a generic contact_id, and we
    add a denormalized player_id for symmetric filtering with
    DocumentDelivery. Phase 1 implemented 1-to-1 between players and
    player_contacts, so this is the right shape.
    """

    __tablename__ = "message_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    player_contact_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("player_contacts.id", ondelete="SET NULL"), nullable=True
    )

    # Snapshots at send time.
    recipient_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipient_phone: Mapped[str | None] = mapped_column(Text, nullable=True)

    channel_used: Mapped[str | None] = mapped_column(Text, nullable=True)  # EMAIL | SMS
    # PENDING | SENT | DELIVERED | BOUNCED | FAILED
    delivery_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="PENDING"
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    message: Mapped[Message] = relationship(
        "Message", lazy="raise", foreign_keys=[message_id]
    )
    organization: Mapped[Organization] = relationship(
        "Organization", lazy="raise", foreign_keys=[organization_id]
    )
    player: Mapped[Player] = relationship(
        "Player", lazy="raise", foreign_keys=[player_id]
    )
    player_contact: Mapped[PlayerContact | None] = relationship(
        "PlayerContact", lazy="raise", foreign_keys=[player_contact_id]
    )

    __table_args__ = (
        Index("idx_msg_deliveries_message", "message_id"),
        Index("idx_msg_deliveries_player_contact", "player_contact_id"),
        Index("idx_msg_deliveries_player", "player_id"),
        Index("idx_msg_deliveries_org", "organization_id"),
        Index("idx_msg_deliveries_status", "delivery_status"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<MessageDelivery id={self.id} message={self.message_id} "
            f"status={self.delivery_status}>"
        )


__all__ = ["Message", "MessageDelivery"]
