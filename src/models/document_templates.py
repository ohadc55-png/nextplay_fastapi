"""DocumentTemplate model — Phase 2.1.

Uploaded PDF or DOCX that an org admin marks up with form fields and
signature zones, then sends to parents via DocumentCampaign. The file
itself lives in S3 (`uploaded_file_url` stores the KEY, not a URL — we
generate presigned URLs on demand). `form_fields` and `signature_zones`
are stored as JSON-as-TEXT via the project's JSONText decorator, so
SQLite (tests) and Postgres (prod) behave identically.

Soft-delete via `is_active=False` — the row stays for historical
deliveries; S3 file is not deleted (no orphan cleanup yet).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
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


class DocumentTemplate(Base):
    __tablename__ = "document_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form bucket — PARTICIPATION | TOURNAMENT | SIZING | HEALTH | PERMISSION | OTHER
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default="OTHER")

    # S3 KEY (not URL). Resolve via s3.presign_get / s3.get_bytes.
    uploaded_file_url: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_file_type: Mapped[str] = mapped_column(Text, nullable=False)  # PDF | DOCX
    uploaded_file_size: Mapped[int] = mapped_column(Integer, nullable=False)  # bytes

    # Form-field definitions. Each entry:
    # {"id": "field1", "type": "text"|"select"|"checkbox"|"date",
    #  "label": "...", "required": bool, "x": int, "y": int,
    #  "width": int, "height": int, "page": int, "options": [...]?}
    form_fields: Mapped[list[dict] | None] = mapped_column(JSONText, nullable=True)
    # Signature-zone definitions. Each entry:
    # {"id": "sig1", "label": "...", "x": int, "y": int, "width": int, "height": int, "page": int}
    signature_zones: Mapped[list[dict] | None] = mapped_column(JSONText, nullable=True)

    # If False → form-fill only, no OTP needed for the parent.
    # default=True so the ORM binds an integer 1 at INSERT — `WHERE is_active
    # IS TRUE` then matches on SQLite (server_default stores the literal
    # string 'true' which SQLite's comparator can't compare to 1).
    # Matches the Phase 1 Player.active pattern.
    requires_signature: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    default_expiry_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )

    # Soft-delete marker. Active templates only by default.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
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
        Index("idx_doc_templates_org", "organization_id"),
        Index("idx_doc_templates_active", "organization_id", "is_active"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DocumentTemplate id={self.id} name={self.name!r} org={self.organization_id}>"


__all__ = ["DocumentTemplate"]
