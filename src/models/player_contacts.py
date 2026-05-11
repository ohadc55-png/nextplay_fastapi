"""PlayerContact model — parent / guardian / medical contact for a player.

Sensitive PII columns are stored under `EncryptedText` (Fernet) so the cell
value at rest is opaque. The parent's name + email stay plaintext because
we need to invite/match on them (parent app, future). The four `*_enc`
columns hold:

- parent_phone_enc: parent or guardian phone number
- national_id_enc: Israeli ת.ז. (or equivalent civil id)
- medical_notes_enc: free text — allergies, conditions, medications
- address_enc: physical address

`organization_id` is denormalized here for fast scoping + RLS. The repository
layer will always filter on it; the route layer enforces the active-org rule.

Cascade: when a player is deleted, their contact row goes with them.
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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base
from src.core.encryption import EncryptedText

if TYPE_CHECKING:
    from src.models.organizations import Organization
    from src.models.players import Player


class PlayerContact(Base):
    """One row per player — sensitive parent + medical info, mostly encrypted.

    UNIQUE(player_id) keeps it 1:1; upsert semantics in the repository.
    """

    __tablename__ = "player_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized for RLS / OrgScopedRepository. Nullable for private-coach
    # players (organization_id IS NULL).
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )

    # === Plaintext (low PII risk; needed for matching / search) ===
    parent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_email: Mapped[str | None] = mapped_column(Text, nullable=True)

    # === Encrypted at rest ===
    parent_phone_enc: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    national_id_enc: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    medical_notes_enc: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    address_enc: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    player: Mapped[Player] = relationship(
        "Player", lazy="raise", foreign_keys=[player_id]
    )
    organization: Mapped[Organization | None] = relationship(
        "Organization", lazy="raise", foreign_keys=[organization_id]
    )

    __table_args__ = (
        UniqueConstraint("player_id", name="uq_player_contacts_player"),
        Index("idx_player_contacts_org", "organization_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PlayerContact id={self.id} player_id={self.player_id}>"


__all__ = ["PlayerContact"]
