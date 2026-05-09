"""Uploads API — DB-only operations.

Async port of `backend/api/uploads.py`. The file-processing pipeline
(PDF/DOCX/XLSX text extraction) lives in Phase 7 alongside the file
processor port. ChromaDB ingestion + KB stats live in Phase 5 alongside
the AI stack. This batch wires the two endpoints that don't depend on
either:
  - DELETE /api/upload/{id}   remove the DB row + flag for cleanup
  - GET /api/kb-stats         stub (returns zeros until Phase 5)

POST /api/upload (file save + content extraction) and POST /api/kb-ingest
(ChromaDB write) are deferred. The frontend will see them as not-yet-
implemented in this batch — they're the last endpoints in the plan.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_user
from src.core.database import get_db
from src.models.uploads import Upload
from src.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["uploads"])


@router.delete("/upload/{upload_id}")
async def delete_upload(
    upload_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tenant-checked delete. The disk file (or S3 object) cleanup is a
    Phase 7 task — for now we drop the DB row and log the orphaned
    filepath so a sweeper can reap it."""
    upload = (await db.execute(
        select(Upload).where(Upload.id == upload_id, Upload.user_id == user.id)
    )).scalar_one_or_none()
    if not upload:
        raise HTTPException(status_code=404, detail="Not found")
    if upload.filepath:
        logger.info(
            "[uploads] deleted DB row id=%s — filepath=%r left for sweeper",
            upload_id, upload.filepath,
        )
    await db.execute(delete(Upload).where(Upload.id == upload_id))
    await db.flush()
    return {"ok": True}


@router.get("/kb-stats")
async def kb_stats(_user: User = Depends(get_current_user)) -> dict:
    """ChromaDB stats stub. Phase 5 wires the real PersistentClient query.
    Returning zeros lets the frontend's "knowledge base" panel render
    without crashing in dev."""
    return {
        "ingested_documents": 0,
        "total_chunks": 0,
        "stub": True,
    }
