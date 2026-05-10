"""Knowledge-base document ingestion — chunk + embed + add to ChromaDB.

Phase 7 batch 6. Async port of `backend/knowledge_base.py` ingestion
functions. The KB *wrapper* (PersistentClient + reranker) lives in
`src/crew/knowledge_base.py` (Phase 5 batch 6) — this module only
covers the ingestion side: read documents, chunk, attach metadata,
push to the collection.

Two ingestion paths:
  - global  — built-in `knowledge_base/documents/` (drills, plays,
              scouting frameworks). All coaches see these.
  - user    — coach-uploaded (future feature). Stamped with
              `(user_id, team_id)` so retrieval can filter.

Idempotency: a JSON manifest at `knowledge_base/.ingested_manifest.json`
records the SHA256 of each ingested file. Re-running ingestion only
processes new or changed files. Bumping `CHUNKING_VERSION` invalidates
the entire manifest (forces re-chunk).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any

from src.crew.knowledge_base import KnowledgeBase, get_kb

logger = logging.getLogger(__name__)


# Match v1 (backend/knowledge_base.py:24-27)
CHUNK_SIZE = 2000     # ~500 tokens (soft target)
CHUNK_OVERLAP = 400   # ~100 tokens (~20% overlap)
CHUNKING_VERSION = 2  # Bump to force re-ingestion across all docs


# ---------------------------------------------------------------------------
# Text cleaning + sentence split + chunking — verbatim from v1
# ---------------------------------------------------------------------------


def clean_text(raw_text: str) -> tuple[str, list[tuple[int, int]]]:
    """Clean PDF-extracted text and build a (offset, page) map.
    Mirrors v1 _clean_text byte-for-byte."""
    page_map: list[tuple[int, int]] = []
    parts = re.split(r"---\s*Page\s+(\d+)\s*---", raw_text)

    cleaned_parts: list[str] = []
    current_page = 1
    offset = 0
    for i, part in enumerate(parts):
        if i % 2 == 1:
            current_page = int(part)
            continue
        if part.strip():
            page_map.append((offset, current_page))
        cleaned_parts.append(part)
        offset += len(part)

    text = "".join(cleaned_parts)
    text = re.sub(r"\d+\s*\|\s*P\s*a\s*g\s*e", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    text = text.strip()

    # Recompute page_map offsets after cleaning (proportional approximation —
    # this is what v1 does; close enough for the per-chunk page tag).
    new_page_map: list[tuple[int, int]] = []
    search_start = 0
    for orig_offset, page_num in page_map:
        if not new_page_map:
            new_page_map.append((0, page_num))
        else:
            new_page_map.append((min(search_start, len(text)), page_num))
        if orig_offset > 0 and offset > 0:
            ratio = len(text) / offset if offset else 1
            search_start = int(orig_offset * ratio)

    return text, new_page_map


def split_sentences(text: str) -> list[str]:
    """Split into sentences via regex. No external deps. Mirrors v1."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])|(?<=\n)\n+", text)
    return [s.strip() for s in parts if s and s.strip()]


def chunk_text(
    text: str,
    *,
    source: str,
    extra_metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Sentence-aware overlapping chunking. Returns
    `[{text, metadata}, ...]`. Mirrors v1 _chunk_text."""
    sentences = split_sentences(text)
    if not sentences:
        return []

    chunks: list[dict[str, Any]] = []
    idx = 0
    current_start = 0

    while current_start < len(sentences):
        chunk_sentences: list[str] = []
        char_count = 0
        i = current_start
        while i < len(sentences):
            sent = sentences[i]
            if char_count + len(sent) > CHUNK_SIZE and chunk_sentences:
                break
            chunk_sentences.append(sent)
            char_count += len(sent) + 1
            i += 1

        body = " ".join(chunk_sentences)
        meta = {"source": source, "chunk_index": idx}
        if extra_metadata:
            meta.update(extra_metadata)
        chunks.append({"text": body, "metadata": meta})

        # Walk back to find ~CHUNK_OVERLAP chars of trailing sentences
        overlap_chars = 0
        overlap_start = i  # default: no overlap
        for j in range(len(chunk_sentences) - 1, -1, -1):
            overlap_chars += len(chunk_sentences[j]) + 1
            if overlap_chars >= CHUNK_OVERLAP:
                overlap_start = current_start + j
                break

        # Always advance at least one sentence (avoid infinite loop)
        if overlap_start <= current_start:
            current_start = i
        else:
            current_start = overlap_start
        idx += 1

    return chunks


# ---------------------------------------------------------------------------
# PDF extraction (full, no cap) — sync; off-thread by callers
# ---------------------------------------------------------------------------


def _extract_pdf_full_sync(filepath: str) -> str:
    """Extract ALL text from a PDF — no truncation. Mirrors v1
    _extract_pdf_full. PyMuPDF first; PyPDF2 fallback."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(filepath)
        text_parts: list[str] = []
        try:
            for i, page in enumerate(doc):
                page_text = page.get_text()
                if page_text.strip():
                    text_parts.append(f"--- Page {i + 1} ---\n{page_text}\n")
        finally:
            doc.close()
        return "".join(text_parts)
    except ImportError:
        try:
            from PyPDF2 import PdfReader

            reader = PdfReader(filepath)
            text_parts = []
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_parts.append(f"--- Page {i + 1} ---\n{page_text}\n")
            return "".join(text_parts)
        except Exception as e:
            logger.debug("[kb] PDF extraction fallback failed: %s", e)
            return ""
    except Exception as e:
        logger.debug("[kb] PDF extraction failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Manifest (idempotent re-runs)
# ---------------------------------------------------------------------------


def _file_hash_sync(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_manifest(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {"version": CHUNKING_VERSION, "files": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # Bump invalidates everything
        if data.get("version") != CHUNKING_VERSION:
            return {"version": CHUNKING_VERSION, "files": {}}
        return data
    except Exception as e:
        logger.warning("[kb] manifest read failed: %s", e)
        return {"version": CHUNKING_VERSION, "files": {}}


def _write_manifest(path: str, data: dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("[kb] manifest write failed: %s", e)


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


async def ingest_pdf(
    filepath: str,
    *,
    kb: KnowledgeBase | None = None,
    user_id: int | None = None,
    team_id: int | None = None,
    scope: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> int:
    """Ingest one PDF file. Returns the chunk count.

    Scope inference:
      - explicit `scope` wins
      - both user_id+team_id present → 'user'
      - otherwise → 'global'

    All chunks attach `(scope, user_id, team_id)` metadata so retrieval
    can filter (downstream — querying is in `src/crew/knowledge_base.py`).
    """
    kb = kb or get_kb()
    filename = os.path.basename(filepath)

    raw_text = await asyncio.to_thread(_extract_pdf_full_sync, filepath)
    if not raw_text:
        logger.warning("[kb] could not extract text from %s", filename)
        return 0

    if scope is None:
        scope = "user" if (user_id is not None and team_id is not None) else "global"
    if scope == "user" and (user_id is None or team_id is None):
        raise ValueError("user-scoped ingest requires both user_id and team_id")

    cleaned, page_map = clean_text(raw_text)

    meta: dict[str, Any] = {"filename": filename, "scope": scope}
    if extra_metadata:
        meta.update(extra_metadata)
    if scope == "user":
        meta["user_id"] = int(user_id)  # type: ignore[arg-type]
        meta["team_id"] = int(team_id)  # type: ignore[arg-type]

    chunks = chunk_text(cleaned, source=filename, extra_metadata=meta)
    if not chunks:
        return 0

    # Enrich each chunk with page number (best-effort lookup)
    if page_map:
        for ch in chunks:
            head = ch["text"][:80]
            chunk_start = cleaned.find(head)
            if chunk_start == -1:
                chunk_start = 0
            page_num = page_map[0][1]
            for pg_offset, pnum in page_map:
                if pg_offset <= chunk_start:
                    page_num = pnum
                else:
                    break
            ch["metadata"]["page"] = page_num

    # Stable IDs — two users uploading the same filename don't collide
    if scope == "user":
        id_prefix = f"u{user_id}_t{team_id}_{filename}"
    else:
        id_prefix = f"global_{filename}"

    ids = [f"{id_prefix}_chunk_{i}" for i in range(len(chunks))]
    documents = [c["text"] for c in chunks]
    metadatas = [c["metadata"] for c in chunks]

    BATCH = 100
    total = 0
    for i in range(0, len(ids), BATCH):
        added = await kb.add_documents(
            ids=ids[i:i + BATCH],
            documents=documents[i:i + BATCH],
            metadatas=metadatas[i:i + BATCH],
        )
        total += added
    return total


async def auto_ingest_documents(
    documents_dir: str,
    *,
    kb: KnowledgeBase | None = None,
    manifest_path: str | None = None,
) -> dict[str, Any]:
    """Walk `documents_dir` and ingest every PDF that's new or changed
    since the last run. Returns `{ingested, skipped, errors, total_chunks}`."""
    documents_dir = str(documents_dir)
    if not os.path.isdir(documents_dir):
        logger.warning("[kb] documents dir does not exist: %s", documents_dir)
        return {"ingested": 0, "skipped": 0, "errors": 0, "total_chunks": 0}

    if manifest_path is None:
        manifest_path = os.path.join(
            os.path.dirname(documents_dir),
            ".ingested_manifest.json",
        )
    manifest = _read_manifest(manifest_path)
    files_state: dict[str, str] = manifest.get("files") or {}

    stats = {"ingested": 0, "skipped": 0, "errors": 0, "total_chunks": 0}

    for entry in sorted(os.listdir(documents_dir)):
        if not entry.lower().endswith(".pdf"):
            continue
        filepath = os.path.join(documents_dir, entry)
        if not os.path.isfile(filepath):
            continue

        try:
            digest = await asyncio.to_thread(_file_hash_sync, filepath)
        except Exception as e:
            logger.warning("[kb] hash failed for %s: %s", entry, e)
            stats["errors"] += 1
            continue

        if files_state.get(entry) == digest:
            stats["skipped"] += 1
            continue

        try:
            chunks = await ingest_pdf(filepath, kb=kb, scope="global")
            stats["ingested"] += 1
            stats["total_chunks"] += chunks
            files_state[entry] = digest
        except Exception as e:
            logger.exception("[kb] ingest failed for %s: %s", entry, e)
            stats["errors"] += 1

    manifest["files"] = files_state
    _write_manifest(manifest_path, manifest)
    return stats


__all__ = [
    "CHUNKING_VERSION",
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "auto_ingest_documents",
    "chunk_text",
    "clean_text",
    "ingest_pdf",
    "split_sentences",
]
