"""
legalize-kr full ingest — Phase 2.

Ingests all 2303 laws from ~/Thairon/legalize-kr/ into ChromaDB.

Source layout:
  ~/Thairon/legalize-kr/kr/<law_folder>/법률.md  (+ 시행령.md, 시행규칙.md)

Article chunking:
  Splits on 제N조 headings (same regex as services/data/legalize_kr.py).
  Each chunk = one ChromaDB document.

Embedding: jhgan/ko-sroberta-multitask (matches Phase 1 stack)
Collection: kolaw_laws (shared with Phase 1 fixture data)
Dedup: law_id + article_number compound key → skip if already present

Progress: prints every 10 laws.

Run:
  python -m services.fast_search.ingest_legalize_kr
  python -m services.fast_search.ingest_legalize_kr --test 100  (test mode)
  CHROMA_PERSIST_PATH=./services/fast_search/chroma_db python -m services.fast_search.ingest_legalize_kr
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# Paths — match services/data/legalize_kr.py defaults
_DEFAULT_CORPUS = Path(os.path.expanduser("~/Thairon/legalize-kr/kr"))
_CORPUS_PATH = Path(os.getenv("LEGALIZE_KR_PATH", str(_DEFAULT_CORPUS)))

# ChromaDB persist path — separate from ephemeral test client
_CHROMA_PERSIST = os.getenv(
    "CHROMA_PERSIST_PATH",
    str(Path(__file__).parent / "chroma_db"),
)

_COLLECTION_NAME = "kolaw_laws"
_LOG_EVERY = 10  # print progress every N laws
_CHUNK_BATCH = 128  # docs per ChromaDB upsert call


# Article splitter — same pattern as legalize_kr.py
_ARTICLE_RE = re.compile(
    r"^#{1,6}\s*(제\d+조(?:의\d+)?(?:\s*\([^)]+\))?)", re.MULTILINE
)


def _parse_frontmatter(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end == -1:
        return meta
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip("'\"")
    return meta


def _split_articles(text: str) -> list[tuple[str, str, str]]:
    """
    Split law Markdown into (article_number, article_title, content) tuples.
    Returns empty list if no 제N조 headings found.
    """
    matches = list(_ARTICLE_RE.finditer(text))
    if not matches:
        # No article headings — treat whole file as one chunk
        return [("전문", "", text.strip()[:4000])]

    articles = []
    for i, match in enumerate(matches):
        heading = match.group(1)
        m = re.match(r"(제\d+조(?:의\d+)?)\s*(\([^)]+\))?", heading)
        if not m:
            continue
        number = m.group(1)
        title = m.group(2) or ""
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            articles.append((number, title, content))
    return articles


def _law_docs(law_dir: Path) -> Iterator[tuple[str, dict, str]]:
    """
    Yield (doc_id, metadata, content) for each article chunk in a law folder.
    Processes 법률.md, 시행령.md, 시행규칙.md if present.
    """
    file_map = {
        "법률": law_dir / "법률.md",
        "시행령": law_dir / "시행령.md",
        "시행규칙": law_dir / "시행규칙.md",
    }

    law_name = law_dir.name
    ingested_at = datetime.now(timezone.utc).isoformat()

    for file_type, md_file in file_map.items():
        if not md_file.exists():
            continue
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Cannot read %s: %s", md_file, exc)
            continue

        meta_raw = _parse_frontmatter(text)
        law_id = meta_raw.get("법령ID", "")
        law_display = meta_raw.get("제목", law_name)
        enforcement = meta_raw.get("시행일자", "").replace("-", "")

        if not law_id:
            # Fallback: use folder name hash as law_id
            law_id = f"folder_{hash(law_name) & 0xFFFFFF:06x}"

        articles = _split_articles(text)
        # Use folder name as part of doc_id to avoid collision across laws with same law_id
        folder_slug = law_name[:20].replace(" ", "_")
        seen_articles: set[str] = set()
        for article_num, article_title, content in articles:
            # law_id + folder_slug + file_type + article_num → globally unique
            base_id = f"{law_id}_{folder_slug}_{file_type}_{article_num}"
            if base_id in seen_articles:
                suffix = 2
                while f"{base_id}_{suffix}" in seen_articles:
                    suffix += 1
                doc_id = f"{base_id}_{suffix}"
            else:
                doc_id = base_id
            seen_articles.add(doc_id)
            metadata = {
                "law_id": law_id,
                "law_name": law_display,
                "law_folder": law_name,
                "file_type": file_type,
                "article": article_num,
                "article_title": article_title,
                "source_file": str(md_file),
                "enforcement_date": enforcement,
                "ingested_at": ingested_at,
            }
            # ChromaDB has 512-char metadata string limit; content in document field
            yield doc_id, metadata, content[:8000]  # truncate very long articles


def get_chroma_client(persist_path: str = _CHROMA_PERSIST):
    """Return a persistent ChromaDB client."""
    import chromadb

    Path(persist_path).mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=persist_path)


def get_embedding_function():
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    return SentenceTransformerEmbeddingFunction(
        model_name="jhgan/ko-sroberta-multitask"
    )


def ingest_legalize_kr(
    corpus_path: Path = _CORPUS_PATH,
    persist_path: str = _CHROMA_PERSIST,
    limit: int | None = None,
) -> dict[str, int]:
    """
    Ingest legalize-kr corpus into ChromaDB.

    Args:
        corpus_path: Path to legalize-kr/kr/ directory.
        persist_path: ChromaDB persist directory.
        limit: Max number of law directories to process (None = all).

    Returns:
        {"laws_processed": N, "docs_ingested": M, "docs_skipped": K}
    """
    if not corpus_path.exists():
        raise FileNotFoundError(
            f"legalize-kr corpus not found at {corpus_path}. "
            "Clone from: github.com/9bow/legalize-kr"
        )

    client = get_chroma_client(persist_path)
    ef = get_embedding_function()
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef,
    )

    law_dirs = sorted(d for d in corpus_path.iterdir() if d.is_dir())
    if limit is not None:
        law_dirs = law_dirs[:limit]

    total_laws = len(law_dirs)
    laws_processed = 0
    docs_ingested = 0
    docs_skipped = 0
    t0 = time.time()

    # Collect in batches for efficiency
    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict] = []
    batch_seen: set[str] = set()  # deduplicate within current batch

    def flush_batch() -> None:
        nonlocal docs_ingested, docs_skipped
        if not batch_ids:
            return
        # Dedup check against existing ChromaDB documents
        existing_ids: set[str] = set()
        try:
            existing = collection.get(ids=batch_ids, include=[])
            existing_ids = set(existing["ids"])
        except Exception:
            pass

        new_ids = [i for i in batch_ids if i not in existing_ids]
        new_docs = [d for i, d in zip(batch_ids, batch_docs) if i not in existing_ids]
        new_metas = [m for i, m in zip(batch_ids, batch_metas) if i not in existing_ids]

        docs_skipped += len(batch_ids) - len(new_ids)
        if new_ids:
            collection.add(ids=new_ids, documents=new_docs, metadatas=new_metas)
            docs_ingested += len(new_ids)

        batch_ids.clear()
        batch_docs.clear()
        batch_metas.clear()
        batch_seen.clear()

    for i, law_dir in enumerate(law_dirs):
        laws_processed += 1
        law_doc_count = 0
        for doc_id, metadata, content in _law_docs(law_dir):
            if doc_id in batch_seen:
                # Collision within batch (shouldn't happen with folder_slug, but guard)
                logger.debug("Skipping duplicate doc_id in batch: %s", doc_id)
                continue
            batch_ids.append(doc_id)
            batch_docs.append(content)
            batch_metas.append(metadata)
            batch_seen.add(doc_id)
            law_doc_count += 1
            if len(batch_ids) >= _CHUNK_BATCH:
                flush_batch()

        if laws_processed % _LOG_EVERY == 0 or laws_processed == total_laws:
            elapsed = time.time() - t0
            rate = laws_processed / elapsed if elapsed > 0 else 0
            eta = (total_laws - laws_processed) / rate if rate > 0 else 0
            print(
                f"[ingest] {laws_processed}/{total_laws} laws "
                f"| ingested={docs_ingested} skipped={docs_skipped} "
                f"| {elapsed:.0f}s elapsed "
                f"| ETA {eta:.0f}s",
                flush=True,
            )

    flush_batch()

    elapsed = time.time() - t0
    print(
        f"[ingest] DONE: {laws_processed} laws, "
        f"{docs_ingested} docs ingested, "
        f"{docs_skipped} skipped, "
        f"{elapsed:.0f}s total"
    )
    return {
        "laws_processed": laws_processed,
        "docs_ingested": docs_ingested,
        "docs_skipped": docs_skipped,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Ingest legalize-kr into ChromaDB")
    parser.add_argument(
        "--test",
        type=int,
        metavar="N",
        default=None,
        help="Test mode: process only first N laws",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=_CORPUS_PATH,
        help=f"Path to legalize-kr/kr/ (default: {_CORPUS_PATH})",
    )
    parser.add_argument(
        "--persist",
        default=_CHROMA_PERSIST,
        help=f"ChromaDB persist path (default: {_CHROMA_PERSIST})",
    )
    args = parser.parse_args()

    result = ingest_legalize_kr(
        corpus_path=args.corpus,
        persist_path=args.persist,
        limit=args.test,
    )
    print(f"Result: {result}")
    sys.exit(0)
