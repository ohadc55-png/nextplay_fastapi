"""Upload schemas. Uploads come in via multipart, not JSON, so the only
shape needed here is the response."""

from __future__ import annotations

from src.schemas.common import ORMModel


class UploadResponse(ORMModel):
    id: int
    user_id: int | None = None
    team_id: int | None = None
    filename: str
    filepath: str
    file_type: str | None = None
    category: str | None = None
    description: str | None = None
    uploaded_at: str | None = None


__all__ = ["UploadResponse"]
