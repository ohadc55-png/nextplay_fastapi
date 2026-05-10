"""ChromaDB knowledge-base wrapper tests.

The KB wraps ChromaDB's sync API in `asyncio.to_thread`. Tests use a
deterministic hash-based embedding function so we never call OpenAI,
and a tmp_path-scoped persist dir so each test is fresh.

Reranker is `None` in tests by default — when set, we stub `CrossEncoder`
via a fake import so the test exercises the reranker path without
downloading a real model.
"""

from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio

from src.crew.knowledge_base import KbHit, KnowledgeBase, reset_kb_for_tests

# ---------------------------------------------------------------------------
# Deterministic embedding fn (8-dim hash → float vector)
# ---------------------------------------------------------------------------


class _FakeEmbedFn:
    """Stable, deterministic, dependency-free embedding function.

    ChromaDB 1.x splits embedding into two methods on the protocol:
      - __call__ / embed_documents: called during .add()
      - embed_query: called during .query()
    We implement both with the same hash-based vector so an exact-text
    match yields distance 0."""

    def __call__(self, input):  # noqa: A002 (chromadb's required arg name)
        out = []
        for text in input:
            digest = hashlib.sha1(text.encode("utf-8")).digest()
            # 8-dim float vector in [-1, 1]
            vec = [(b - 128) / 128.0 for b in digest[:8]]
            out.append(vec)
        return out

    def embed_query(self, input):  # noqa: A002
        return self.__call__(input)

    # Newer chromadb versions inspect `name()` for cache fingerprinting
    def name(self) -> str:
        return "fake_embed_fn_v1"


@pytest_asyncio.fixture
async def kb(tmp_path):
    """Fresh KB rooted in tmp_path with a fake embedding fn and no
    reranker. Cleared between tests via the fixture's tmp_path."""
    reset_kb_for_tests()
    instance = KnowledgeBase(
        persist_dir=str(tmp_path / "chroma_store"),
        collection_name="test_collection",
        embedding_fn=_FakeEmbedFn(),
        reranker_model=None,
    )
    yield instance


# ---------------------------------------------------------------------------
# Round-trip add → query
# ---------------------------------------------------------------------------


class TestRoundTrip:
    async def test_count_starts_at_zero(self, kb: KnowledgeBase):
        assert await kb.count() == 0
        assert await kb.is_ready() is False

    async def test_add_then_count(self, kb: KnowledgeBase):
        added = await kb.add_documents(
            ids=["d1", "d2"],
            documents=[
                "Pick and roll fundamentals",
                "Zone defense rotations",
            ],
            metadatas=[{"topic": "offense"}, {"topic": "defense"}],
        )
        assert added == 2
        assert await kb.count() == 2
        assert await kb.is_ready() is True

    async def test_query_returns_indexed_doc(self, kb: KnowledgeBase):
        await kb.add_documents(
            ids=["d1"],
            documents=["Pick and roll fundamentals"],
            metadatas=[{"topic": "offense"}],
        )
        # Same input text → same fake vector → exact match (distance 0)
        hits = await kb.search("Pick and roll fundamentals", limit=3)
        assert len(hits) == 1
        assert hits[0].id == "d1"
        assert hits[0].document == "Pick and roll fundamentals"
        assert hits[0].metadata == {"topic": "offense"}
        # Distance is 0 for exact-text match with our deterministic fn
        assert hits[0].distance == pytest.approx(0.0, abs=1e-6)

    async def test_query_on_empty_collection(self, kb: KnowledgeBase):
        hits = await kb.search("anything", limit=3)
        assert hits == []

    async def test_empty_query_returns_empty(self, kb: KnowledgeBase):
        await kb.add_documents(ids=["d1"], documents=["Pick and roll"])
        assert await kb.search("") == []
        assert await kb.search("   ") == []

    async def test_limit_caps_results(self, kb: KnowledgeBase):
        await kb.add_documents(
            ids=[f"d{i}" for i in range(10)],
            documents=[f"Drill {i}" for i in range(10)],
        )
        hits = await kb.search("Drill", limit=3)
        assert len(hits) <= 3

    async def test_kbhit_as_dict_roundtrip(self):
        h = KbHit(id="x", document="doc", distance=0.42, metadata={"k": "v"})
        d = h.as_dict()
        assert d == {"id": "x", "document": "doc", "distance": 0.42, "metadata": {"k": "v"}}
        # rerank_score is omitted when None
        assert "rerank_score" not in d
        h.rerank_score = 0.9
        assert h.as_dict()["rerank_score"] == 0.9


# ---------------------------------------------------------------------------
# Reranker — graceful degrade + reorder
# ---------------------------------------------------------------------------


class TestReranker:
    async def test_missing_sentence_transformers_degrades_gracefully(
        self, tmp_path, monkeypatch
    ):
        """If sentence_transformers can't be imported (or model loading
        fails), the KB falls back to ChromaDB's distance ordering and
        logs a warning. No exception escapes."""

        # Force ImportError when the KB tries to load the reranker
        def _raise(*_args, **_kwargs):
            raise ImportError("sentence_transformers unavailable")

        monkeypatch.setitem(sys.modules, "sentence_transformers", None)

        kb = KnowledgeBase(
            persist_dir=str(tmp_path / "chroma"),
            collection_name="rerank_missing",
            embedding_fn=_FakeEmbedFn(),
            reranker_model="cross-encoder/some-model",
        )
        await kb.add_documents(ids=["d1"], documents=["zone defense"])
        hits = await kb.search("zone defense", limit=1)
        assert len(hits) == 1
        # No rerank_score because the reranker never loaded
        assert hits[0].rerank_score is None

    async def test_reranker_reorders_by_score(self, tmp_path, monkeypatch):
        """Stub CrossEncoder so .predict() returns scores that flip
        the ChromaDB-distance order. Verifies the KB respects rerank
        scores when present."""
        captured_pairs: list[tuple[str, str]] = []

        class _StubCrossEncoder:
            def __init__(self, model_name):
                pass

            def predict(self, pairs):
                captured_pairs.extend(pairs)
                # Deliberately reverse the score order: last doc gets
                # highest score so it ends up first after sort.
                return list(range(len(pairs)))

        # Inject a fake `sentence_transformers` module
        fake_mod = SimpleNamespace(CrossEncoder=_StubCrossEncoder)
        monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

        kb = KnowledgeBase(
            persist_dir=str(tmp_path / "chroma_rerank"),
            collection_name="t_rerank",
            embedding_fn=_FakeEmbedFn(),
            reranker_model="cross-encoder/some-model",
        )
        await kb.add_documents(
            ids=["a", "b", "c"],
            documents=["alpha drill", "beta drill", "gamma drill"],
        )
        # Query for something far from any doc so all 3 hit with non-zero distance
        hits = await kb.search("zzz", limit=3)
        # Predict was called once with one pair per hit
        assert len(captured_pairs) == len(hits)
        # Highest rerank_score should now be first
        assert hits[0].rerank_score == max(h.rerank_score for h in hits)
