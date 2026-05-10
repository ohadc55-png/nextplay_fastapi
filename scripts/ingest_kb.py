"""KB ingestion CLI — `python -m scripts.ingest_kb`.

Walks `knowledge_base/documents/` and adds new/changed PDFs to the
ChromaDB collection. Idempotent — already-ingested files are skipped
based on a SHA256 manifest at `knowledge_base/.ingested_manifest.json`.

Usage:
    # Default — uses settings.CHROMA_PERSIST_DIR + ./knowledge_base/documents
    python -m scripts.ingest_kb

    # Explicit paths
    python -m scripts.ingest_kb --documents ./alt/documents \\
        --manifest ./alt/.ingested_manifest.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow `python -m scripts.ingest_kb` to find src/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _main(documents_dir: str, manifest_path: str | None) -> int:
    from src.crew.knowledge_base import get_kb
    from src.services.kb_ingest import auto_ingest_documents

    kb = get_kb()
    print(f"[kb-ingest] documents:    {documents_dir}")
    print(f"[kb-ingest] persist_dir:  {kb.persist_dir}")
    print(f"[kb-ingest] collection:   {kb.collection_name}")
    print()

    stats = await auto_ingest_documents(
        documents_dir, kb=kb, manifest_path=manifest_path,
    )
    print(f"[kb-ingest] ingested:     {stats['ingested']}")
    print(f"[kb-ingest] skipped:      {stats['skipped']}")
    print(f"[kb-ingest] errors:       {stats['errors']}")
    print(f"[kb-ingest] total chunks: {stats['total_chunks']}")
    print(f"[kb-ingest] kb count:     {await kb.count()}")
    return 0 if stats["errors"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest PDFs into the ChromaDB knowledge base.",
    )
    parser.add_argument(
        "--documents",
        default=str(
            Path(__file__).resolve().parents[1] / "knowledge_base" / "documents"
        ),
        help="Directory containing PDFs to ingest.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to the ingested-manifest JSON (default: alongside --documents).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(_main(args.documents, args.manifest))


if __name__ == "__main__":
    raise SystemExit(main())
