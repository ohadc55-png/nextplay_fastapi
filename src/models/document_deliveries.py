"""DocumentDelivery model — Phase 2.1.

One row per recipient (per player) inside a campaign. Carries the
unique token a parent uses to access the public signing page, plus the
delivery + document-interaction state machines, plus the final signed
PDF reference and an audit_data blob.

`recipient_phone` is stored DECRYPTED here intentionally — we need it
plaintext to send SMS, and the encrypted source lives in
PlayerContact.parent_phone_enc. Every snapshot read of the contact's
phone is logged via log_org_action(action="player.contact.read").

The token is a 32-char hex string (UUID4 → hex). We never expose `id`
to the parent.
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
    from src.models.document_campaigns import DocumentCampaign
    from src.models.organizations import Organization
    from src.models.player_contacts import PlayerContact
    from src.models.players import Player


class DocumentDelivery(Base):
    __tablename__ = "document_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("document_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    # Snapshot of the PlayerContact at send time. SET NULL because the
    # contact row might be deleted while the delivery is still readable.
    player_contact_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("player_contacts.id", ondelete="SET NULL"), nullable=True
    )

    # Snapshot fields — preserve recipient info even if PlayerContact is mutated.
    recipient_name: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipient_phone: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 32-char hex (UUID4). Unique across the entire table — that's how
    # `/sign/{token}` finds the row in O(1).
    unique_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # Delivery channel state machine.
    # PENDING | SENT_EMAIL | SENT_SMS | DELIVERED | BOUNCED | FAILED
    delivery_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="PENDING"
    )
    channel_used: Mapped[str | None] = mapped_column(Text, nullable=True)  # EMAIL | SMS
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Document-interaction state machine.
    # NOT_OPENED | OPENED | FILLED | SIGNED | DECLINED | EXPIRED
    document_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="NOT_OPENED"
    )
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Parent's filled-out form (keys match template.form_fields[].id).
    form_response: Mapped[dict | None] = mapped_column(JSONText, nullable=True)

    # Signature artifacts.
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signature_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # S3 KEY
    signature_method: Mapped[str | None] = mapped_column(Text, nullable=True)  # DRAWN | TYPED

    # Final signed PDF (S3 KEY).
    final_pdf_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Audit blob: {ip, user_agent, otp_verified_at, payload_hash, ...}.
    # The optional hash-chain (prev_hash, self_hash) lands in Part B.
    audit_data: Mapped[dict | None] = mapped_column(JSONText, nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    last_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminders_sent_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    campaign: Mapped[DocumentCampaign] = relationship(
        "DocumentCampaign", lazy="raise", foreign_keys=[campaign_id]
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
        Index("idx_doc_deliveries_campaign", "campaign_id"),
        Index("idx_doc_deliveries_token", "unique_token"),  # token lookup hot path
        Index("idx_doc_deliveries_player", "player_id"),
        Index("idx_doc_deliveries_status", "document_status"),
        Index("idx_doc_deliveries_org", "organization_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<DocumentDelivery id={self.id} status={self.document_status} "
            f"campaign={self.campaign_id}>"
        )


__all__ = ["DocumentDelivery"]
