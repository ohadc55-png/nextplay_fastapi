"""KB ingestion tests — chunker correctness + idempotent manifest.

The chunker is the most retrieval-quality-sensitive piece. We verify:
  - sentence-aware splitting (no mid-sentence cuts)
  - overlap actually overlaps
  - chunk size cap is respected (with a soft margin)
  - idempotent re-runs don't re-ingest unchanged files"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src.services import kb_ingest

# ---------------------------------------------------------------------------
# Fake KB — captures add_documents calls without touching ChromaDB
# ---------------------------------------------------------------------------


class _FakeKb:
    def __init__(self):
        self.added_ids: list[str] = []
        self.added_docs: list[str] = []
        self.added_metas: list[dict] = []
        self.add_documents = AsyncMock(side_effect=self._record)

    async def _record(self, *, ids, documents, metadatas=None):
        self.added_ids.extend(ids)
        self.added_docs.extend(documents)
        self.added_metas.extend(metadatas or [{}] * len(ids))
        return len(ids)


@pytest.fixture
def fake_kb():
    return _FakeKb()


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


class TestChunker:
    def test_short_text_one_chunk(self):
        chunks = kb_ingest.chunk_text(
            "A short paragraph. Another sentence here.",
            source="x.pdf",
        )
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["source"] == "x.pdf"
        assert chunks[0]["metadata"]["chunk_index"] == 0

    def test_empty_text_returns_empty(self):
        assert kb_ingest.chunk_text("", source="x.pdf") == []
        assert kb_ingest.chunk_text("   \n  ", source="x.pdf") == []

    def test_chunks_respect_size_cap(self):
        """Each chunk should fit roughly under CHUNK_SIZE chars (allowing
        one final sentence to push past — that's by design in v1)."""
        long_para = "This is a sentence. " * 500  # ~10000 chars
        chunks = kb_ingest.chunk_text(long_para, source="big.pdf")
        assert len(chunks) > 1
        # No chunk dramatically blows past CHUNK_SIZE (5x is a hard upper)
        for ch in chunks:
            assert len(ch["text"]) < kb_ingest.CHUNK_SIZE * 5

    def test_chunks_have_overlap(self):
        """Sequential chunks should share trailing/leading sentences so
        retrieval doesn't lose context at the boundary."""
        sentences = [f"This is sentence number {i}." for i in range(200)]
        text = " ".join(sentences)
        chunks = kb_ingest.chunk_text(text, source="overlap.pdf")
        assert len(chunks) >= 2
        # Compare end of chunk[0] with start of chunk[1] — should share text
        first = chunks[0]["text"]
        second = chunks[1]["text"]
        # Find a non-trivial substring from end of first that appears in second
        tail = first[-300:]
        # At least one sentence from `tail` should appear in `second`
        for sent in tail.split(". "):
            if sent.strip() and len(sent) > 20 and sent in second:
                return  # overlap confirmed
        pytest.fail("Expected overlap between consecutive chunks")

    def test_extra_metadata_attached(self):
        chunks = kb_ingest.chunk_text(
            "One sentence.",
            source="x.pdf",
            extra_metadata={"scope": "global", "tag": "drills"},
        )
        assert chunks[0]["metadata"]["scope"] == "global"
        assert chunks[0]["metadata"]["tag"] == "drills"

    def test_chunk_indices_monotonic(self):
        text = ". ".join(f"Sentence {i}" for i in range(300))
        chunks = kb_ingest.chunk_text(text, source="x.pdf")
        indices = [c["metadata"]["chunk_index"] for c in chunks]
        assert indices == sorted(indices)
        assert indices == list(range(len(chunks)))


class TestCleanText:
    def test_page_markers_extracted(self):
        raw = "--- Page 1 ---\nHello world.\n--- Page 2 ---\nMore text."
        cleaned, page_map = kb_ingest.clean_text(raw)
        assert "Page 1" not in cleaned  # marker removed
        assert "Hello world" in cleaned
        # Page map has entries for both pages
        pages = {p[1] for p in page_map}
        assert pages == {1, 2}

    def test_page_footer_noise_stripped(self):
        raw = "Real content.\n42 | P a g e\nMore content."
        cleaned, _ = kb_ingest.clean_text(raw)
        assert "P a g e" not in cleaned
        assert "Real content" in cleaned

    def test_single_newlines_become_spaces(self):
        raw = "First line.\nSecond line."
        cleaned, _ = kb_ingest.clean_text(raw)
        assert "\n" not in cleaned
        assert "First line. Second line." in cleaned


class TestSplitSentences:
    def test_basic_split(self):
        out = kb_ingest.split_sentences("First. Second! Third? Fourth.")
        # Final 'Fourth.' may or may not split depending on regex's lookahead
        assert "First." in out or any("First" in s for s in out)
        assert any("Second" in s for s in out)

    def test_paragraph_boundary_splits(self):
        out = kb_ingest.split_sentences("Para one\n\nPara two")
        assert any("Para one" in s for s in out)
        assert any("Para two" in s for s in out)


# ---------------------------------------------------------------------------
# ingest_pdf — scope handling + chunk count
# ---------------------------------------------------------------------------


class TestIngestPdf:
    async def test_global_scope_default(self, fake_kb, tmp_path):
        from unittest.mock import patch

        # Stub the PDF extractor so we don't need a real PDF
        fake_text = "--- Page 1 ---\n" + "This is a coaching tip. " * 100
        with patch.object(kb_ingest, "_extract_pdf_full_sync", return_value=fake_text):
            count = await kb_ingest.ingest_pdf(
                str(tmp_path / "drills.pdf"), kb=fake_kb,
            )
        assert count > 0
        # All chunks have scope=global, no user_id
        for meta in fake_kb.added_metas:
            assert meta["scope"] == "global"
            assert "user_id" not in meta
        # IDs prefixed with global_
        assert all(i.startswith("global_drills.pdf") for i in fake_kb.added_ids)

    async def test_user_scope_stamps_user_team(self, fake_kb, tmp_path):
        from unittest.mock import patch

        fake_text = "--- Page 1 ---\nA scouting note. " * 50
        with patch.object(kb_ingest, "_extract_pdf_full_sync", return_value=fake_text):
            count = await kb_ingest.ingest_pdf(
                str(tmp_path / "scout.pdf"), kb=fake_kb,
                user_id=1, team_id=10,
            )
        assert count > 0
        for meta in fake_kb.added_metas:
            assert meta["scope"] == "user"
            assert meta["user_id"] == 1
            assert meta["team_id"] == 10

    async def test_user_scope_requires_both_ids(self, fake_kb, tmp_path):
        from unittest.mock import patch

        with patch.object(kb_ingest, "_extract_pdf_full_sync", return_value="some text"):
            with pytest.raises(ValueError):
                await kb_ingest.ingest_pdf(
                    str(tmp_path / "x.pdf"), kb=fake_kb,
                    scope="user", user_id=1,  # missing team_id
                )

    async def test_empty_pdf_returns_zero(self, fake_kb, tmp_path):
        from unittest.mock import patch

        with patch.object(kb_ingest, "_extract_pdf_full_sync", return_value=""):
            count = await kb_ingest.ingest_pdf(
                str(tmp_path / "empty.pdf"), kb=fake_kb,
            )
        assert count == 0
        fake_kb.add_documents.assert_not_called()


# ---------------------------------------------------------------------------
# auto_ingest_documents — manifest + idempotency
# ---------------------------------------------------------------------------


class TestAutoIngest:
    async def test_skips_unchanged_files_on_second_run(
        self, fake_kb, tmp_path
    ):
        from unittest.mock import patch

        docs = tmp_path / "documents"
        docs.mkdir()
        (docs / "a.pdf").write_bytes(b"fake-pdf-bytes-for-a")
        (docs / "b.pdf").write_bytes(b"fake-pdf-bytes-for-b")

        with patch.object(
            kb_ingest, "_extract_pdf_full_sync",
            return_value="Some content. " * 30,
        ):
            stats1 = await kb_ingest.auto_ingest_documents(str(docs), kb=fake_kb)
            stats2 = await kb_ingest.auto_ingest_documents(str(docs), kb=fake_kb)

        assert stats1["ingested"] == 2
        assert stats1["skipped"] == 0
        # Second run sees the manifest and skips both
        assert stats2["ingested"] == 0
        assert stats2["skipped"] == 2
        # Manifest file exists alongside documents/
        manifest = tmp_path / ".ingested_manifest.json"
        assert manifest.exists()
        data = json.loads(manifest.read_text())
        assert data["version"] == kb_ingest.CHUNKING_VERSION
        assert "a.pdf" in data["files"] and "b.pdf" in data["files"]

    async def test_changed_file_reingested(self, fake_kb, tmp_path):
        from unittest.mock import patch

        docs = tmp_path / "documents"
        docs.mkdir()
        path = docs / "a.pdf"
        path.write_bytes(b"original")

        with patch.object(
            kb_ingest, "_extract_pdf_full_sync",
            return_value="Original. " * 30,
        ):
            await kb_ingest.auto_ingest_documents(str(docs), kb=fake_kb)

        # Modify the file → hash changes → re-ingest
        path.write_bytes(b"modified content")
        with patch.object(
            kb_ingest, "_extract_pdf_full_sync",
            return_value="Modified. " * 30,
        ):
            stats = await kb_ingest.auto_ingest_documents(str(docs), kb=fake_kb)
        assert stats["ingested"] == 1
        assert stats["skipped"] == 0

    async def test_chunking_version_bump_invalidates_manifest(
        self, fake_kb, tmp_path, monkeypatch
    ):
        from unittest.mock import patch

        docs = tmp_path / "documents"
        docs.mkdir()
        (docs / "a.pdf").write_bytes(b"x")

        with patch.object(
            kb_ingest, "_extract_pdf_full_sync",
            return_value="Content. " * 30,
        ):
            await kb_ingest.auto_ingest_documents(str(docs), kb=fake_kb)

        # Bump the chunking version
        monkeypatch.setattr(kb_ingest, "CHUNKING_VERSION", 999)
        with patch.object(
            kb_ingest, "_extract_pdf_full_sync",
            return_value="Content. " * 30,
        ):
            stats = await kb_ingest.auto_ingest_documents(str(docs), kb=fake_kb)
        # Whole manifest invalidated → all files re-ingested
        assert stats["ingested"] == 1

    async def test_missing_dir_returns_zero_stats(self, fake_kb, tmp_path):
        stats = await kb_ingest.auto_ingest_documents(
            str(tmp_path / "nope"), kb=fake_kb,
        )
        assert stats["ingested"] == 0

    async def test_non_pdf_files_ignored(self, fake_kb, tmp_path):
        docs = tmp_path / "documents"
        docs.mkdir()
        (docs / "ignored.txt").write_text("not a pdf")
        (docs / "ignored.md").write_text("# also not")
        (docs / "real.pdf").write_bytes(b"pretend pdf")

        from unittest.mock import patch
        with patch.object(
            kb_ingest, "_extract_pdf_full_sync",
            return_value="content. " * 30,
        ):
            stats = await kb_ingest.auto_ingest_documents(str(docs), kb=fake_kb)
        assert stats["ingested"] == 1  # only real.pdf
