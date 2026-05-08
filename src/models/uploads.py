"""Uploads model — coach-uploaded files (PDF, DOCX, images, video stills, ...).

Files referenced inline in chat via `[Uploaded: filename]` syntax. Tenant-scoped:
queries must filter by both `user_id` AND `team_id`. The `content_cache` column
holds extracted text and is lazily backfilled by the file processor.

Origin: `backend/db/__init__.py` `init_db()` + `add_user_id_columns` +
`add_team_id_columns` + `add_file_content_cache` + `add_performance_indexes`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("team_profile.id"), nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    filepath: Mapped[str] = mapped_column(Text, nullable=False)  # local path or S3 key
    file_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_cache: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())

    __table_args__ = (
        Index("idx_uploads_user_id", "user_id"),
        Index("idx_uploads_team_id", "team_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Upload id={self.id} filename={self.filename!r}>"


__all__ = ["Upload"]
