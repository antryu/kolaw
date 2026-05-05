import pytest
pytestmark = pytest.mark.skip(reason="Phase 1 ChromaDB / mock-RLM tests; superseded by Phase 3 architecture (services.data.legalize_kr.grep_search + services.data.law_go_kr.LawGoKrClient)")

"""
test_ingest_legalize_kr.py — Phase 2 ingest tests.

Test mode: processes first 100 law directories from legalize-kr corpus.
Verifies ChromaDB count increases after ingest.
Verifies dedup (second run skips all).
"""

import os
import tempfile
from pathlib import Path

import pytest

from services.fast_search.ingest_legalize_kr import (
    _CORPUS_PATH,
    ingest_legalize_kr,
)

_CORPUS_AVAILABLE = _CORPUS_PATH.exists()
_TEST_LIMIT = 100  # process first 100 laws for test speed


@pytest.mark.skipif(not _CORPUS_AVAILABLE, reason="legalize-kr corpus not mounted")
def test_ingest_100_laws_increases_count():
    """Ingesting 100 laws should add docs to ChromaDB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = ingest_legalize_kr(
            corpus_path=_CORPUS_PATH,
            persist_path=tmpdir,
            limit=_TEST_LIMIT,
        )
        assert result["laws_processed"] == _TEST_LIMIT
        assert result["docs_ingested"] > 0, "Expected at least 1 doc ingested"
        # Each law has at least 법률.md with at least 1 article
        assert result["docs_ingested"] >= _TEST_LIMIT


@pytest.mark.skipif(not _CORPUS_AVAILABLE, reason="legalize-kr corpus not mounted")
def test_ingest_dedup_skips_existing():
    """Second ingest of same data should skip all (dedup by law_id+article)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result1 = ingest_legalize_kr(
            corpus_path=_CORPUS_PATH,
            persist_path=tmpdir,
            limit=10,
        )
        first_ingested = result1["docs_ingested"]
        assert first_ingested > 0

        result2 = ingest_legalize_kr(
            corpus_path=_CORPUS_PATH,
            persist_path=tmpdir,
            limit=10,
        )
        assert result2["docs_ingested"] == 0, "Dedup failed: should skip all on second run"
        assert result2["docs_skipped"] == first_ingested


@pytest.mark.skipif(not _CORPUS_AVAILABLE, reason="legalize-kr corpus not mounted")
def test_ingest_chroma_count_matches():
    """ChromaDB collection count should reflect ingested docs."""
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    with tempfile.TemporaryDirectory() as tmpdir:
        result = ingest_legalize_kr(
            corpus_path=_CORPUS_PATH,
            persist_path=tmpdir,
            limit=5,
        )
        client = chromadb.PersistentClient(path=tmpdir)
        ef = SentenceTransformerEmbeddingFunction(model_name="jhgan/ko-sroberta-multitask")
        collection = client.get_collection(name="kolaw_laws", embedding_function=ef)
        assert collection.count() == result["docs_ingested"]


def test_ingest_missing_corpus_raises():
    """FileNotFoundError if corpus path does not exist."""
    with pytest.raises(FileNotFoundError, match="legalize-kr corpus not found"):
        ingest_legalize_kr(corpus_path=Path("/nonexistent/legalize-kr/kr"))


@pytest.mark.skipif(not _CORPUS_AVAILABLE, reason="legalize-kr corpus not mounted")
def test_ingest_metadata_fields():
    """Ingested docs should have required metadata fields."""
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    with tempfile.TemporaryDirectory() as tmpdir:
        ingest_legalize_kr(corpus_path=_CORPUS_PATH, persist_path=tmpdir, limit=3)
        client = chromadb.PersistentClient(path=tmpdir)
        ef = SentenceTransformerEmbeddingFunction(model_name="jhgan/ko-sroberta-multitask")
        collection = client.get_collection(name="kolaw_laws", embedding_function=ef)
        results = collection.get(limit=5, include=["metadatas"])
        for meta in results["metadatas"]:
            assert "law_id" in meta
            assert "law_name" in meta
            assert "article" in meta
            assert "source_file" in meta
            assert "ingested_at" in meta
