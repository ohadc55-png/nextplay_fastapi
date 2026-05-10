"""Basketball coaching knowledge base — ChromaDB wrapper.

What this module owns:
  - A single PersistentClient at `settings.CHROMA_PERSIST_DIR` (no
    network — vectors live on disk so cold starts skip embedding).
  - One collection (`settings.CHROMA_COLLECTION`) with OpenAI
    `text-embedding-3-small` embeddings.
  - An optional cross-encoder reranker (sentence-transformers
    `ms-marco-MiniLM-L-12-v2`) that re-orders the top-N candidates by
    relevance scores. Loading is best-effort: if the model isn't
    cached and the network is offline (or sentence-transformers fails
    on import), we fall back to ChromaDB's distance-based ordering.

Why async wrappers? ChromaDB's API is synchronous and CPU-bound (HNSW
graph + sentence-transformers inference). Calling it directly from a
FastAPI handler blocks the event loop and starves every other coach
sharing the worker. Every public method delegates to
`asyncio.to_thread` so the loop keeps spinning.

Multi-tenancy note: the KB is **shared across all coaches** — it
contains generic basketball coaching content (drills, plays, scouting
frameworks). It is NOT a coach-private store. Coach-private vectors
live in `memories.embedding_json` (separate path, ported in batch 7).

What's deferred to Phase 7 / later:
  - Document ingestion (the script that reads PDFs/docs from
    `knowledge_base/documents/` and chunks → embeds → adds them).
  - Per-tenant filters (we don't need them — KB is shared).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from src.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class KbHit:
    """One search result. Mirrors v1 shape so the prompt-injection
    template doesn't need rewriting when CrewAI orchestration lands."""
    id: str
    document: str
    distance: float
    metadata: dict[str, Any] = field(default_factory=dict)
    rerank_score: float | None = None  # only set when reranker ran

    def as_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "document": self.document,
            "distance": self.distance,
            "metadata": self.metadata,
        }
        if self.rerank_score is not None:
            out["rerank_score"] = self.rerank_score
        return out


# ---------------------------------------------------------------------------
# KnowledgeBase
# ---------------------------------------------------------------------------


class KnowledgeBase:
    """Thin async wrapper around a ChromaDB PersistentClient.

    Construction is cheap — the client connects to the persist
    directory but doesn't load reranker or pull embeddings until first
    use. `is_ready` reports whether the collection has any documents
    so the chat surface can degrade gracefully when the KB is empty.
    """

    def __init__(
        self,
        *,
        persist_dir: str,
        collection_name: str,
        embedding_fn: Any | None = None,
        reranker_model: str | None = None,
    ) -> None:
        self.persist_dir = persist_dir
        self.collection_name = collection_name
        self._embedding_fn = embedding_fn
        self._reranker_model_name = reranker_model
        self._client: Any | None = None
        self._collection: Any | None = None
        self._reranker: Any | None = None
        self._reranker_failed = False
        self._init_error: str | None = None

    # --- internal sync helpers (run inside asyncio.to_thread) ----------

    def _connect_sync(self) -> tuple[Any, Any]:
        """Open the PersistentClient and get-or-create the collection.
        Lazy: invoked only on the first call that needs the collection."""
        import chromadb  # local import — heavyweight

        if self._client is None:
            self._client = chromadb.PersistentClient(path=self.persist_dir)
        if self._collection is None:
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self._embedding_fn,
            )
        return self._client, self._collection

    def _load_reranker_sync(self) -> Any | None:
        """Best-effort load. Returns None if sentence-transformers is
        not importable, or the model can't be downloaded/cached."""
        if self._reranker is not None or self._reranker_failed:
            return self._reranker
        if not self._reranker_model_name:
            return None
        try:
            from sentence_transformers import CrossEncoder

            self._reranker = CrossEncoder(self._reranker_model_name)
            return self._reranker
        except Exception as e:
            logger.warning(
                "[kb] reranker unavailable (%s) — falling back to distance ordering",
                e,
            )
            self._reranker_failed = True
            return None

    def _query_sync(self, query: str, n_results: int) -> list[KbHit]:
        _client, collection = self._connect_sync()
        if collection.count() == 0:
            return []

        # Pull more candidates than needed when reranking — gives the
        # cross-encoder room to reorder. Otherwise the reranker can
        # only confirm the order ChromaDB already chose.
        candidate_n = max(n_results * 4, n_results)
        raw = collection.query(
            query_texts=[query],
            n_results=candidate_n,
        )

        # ChromaDB returns each field as `[[<row1>, <row2>, ...]]`
        # because query supports batches; we always send 1 query.
        ids = (raw.get("ids") or [[]])[0]
        docs = (raw.get("documents") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]
        metas = (raw.get("metadatas") or [[]])[0] or [{} for _ in ids]

        hits = [
            KbHit(id=i, document=d, distance=dist, metadata=m or {})
            for i, d, dist, m in zip(ids, docs, distances, metas, strict=False)
        ]

        reranker = self._load_reranker_sync()
        if reranker is not None and hits:
            pairs = [(query, h.document) for h in hits]
            try:
                scores = reranker.predict(pairs)
            except Exception as e:
                logger.warning("[kb] reranker.predict failed: %s — keeping distance order", e)
                return hits[:n_results]
            for h, s in zip(hits, scores, strict=False):
                h.rerank_score = float(s)
            hits.sort(key=lambda h: -(h.rerank_score or 0.0))

        return hits[:n_results]

    def _add_sync(
        self,
        *,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> int:
        _client, collection = self._connect_sync()
        if not ids or not documents:
            return 0
        # ChromaDB 1.x rejects empty metadata dicts. Drop the kwarg
        # entirely when the caller has nothing meaningful to attach;
        # the ingestion script will pass real metadata (source URL,
        # chunk index, etc.) when it lands.
        if metadatas and any(m for m in metadatas):
            collection.add(ids=ids, documents=documents, metadatas=metadatas)
        else:
            collection.add(ids=ids, documents=documents)
        return len(ids)

    def _count_sync(self) -> int:
        try:
            _client, collection = self._connect_sync()
            return int(collection.count())
        except Exception as e:
            self._init_error = str(e)
            logger.warning("[kb] count failed: %s", e)
            return 0

    # --- async API ------------------------------------------------------

    async def search(self, query: str, limit: int = 5) -> list[KbHit]:
        q = (query or "").strip()
        if not q:
            return []
        n = max(1, min(int(limit or 5), 20))
        return await asyncio.to_thread(self._query_sync, q, n)

    async def add_documents(
        self,
        *,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self._add_sync,
            ids=ids, documents=documents, metadatas=metadatas,
        )

    async def count(self) -> int:
        return await asyncio.to_thread(self._count_sync)

    async def is_ready(self) -> bool:
        """True if the persistent store is reachable AND the collection
        has at least one document. Used by the tool wrapper to decide
        whether to call out to the KB or emit a friendly 'empty' note."""
        n = await self.count()
        return n > 0


# ---------------------------------------------------------------------------
# Module-level singleton + factory
# ---------------------------------------------------------------------------


_kb_instance: KnowledgeBase | None = None


def _build_embedding_fn() -> Any | None:
    """Construct the OpenAI embedding function from settings.

    Returns None when no API key is configured — ChromaDB falls back
    to its built-in `all-MiniLM-L6-v2` which runs locally. That keeps
    the KB usable on dev machines without OPENAI_API_KEY set."""
    if not settings.OPENAI_API_KEY:
        return None
    try:
        from chromadb.utils import embedding_functions

        return embedding_functions.OpenAIEmbeddingFunction(
            api_key=settings.OPENAI_API_KEY,
            model_name=settings.EMBEDDING_MODEL,
        )
    except Exception as e:
        logger.warning("[kb] could not build OpenAI embedding fn: %s", e)
        return None


def get_kb() -> KnowledgeBase:
    """Return the process-wide KB singleton. Lazy — the actual
    PersistentClient connection is opened on first use, not here."""
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = KnowledgeBase(
            persist_dir=settings.CHROMA_PERSIST_DIR,
            collection_name=settings.CHROMA_COLLECTION,
            embedding_fn=_build_embedding_fn(),
            reranker_model=settings.RERANKER_MODEL or None,
        )
    return _kb_instance


def reset_kb_for_tests() -> None:
    """Tests that swap settings (e.g. tmp_path persist_dir) need to
    invalidate the cached singleton so the next get_kb() builds afresh."""
    global _kb_instance
    _kb_instance = None


__all__ = ["KbHit", "KnowledgeBase", "get_kb", "reset_kb_for_tests"]
