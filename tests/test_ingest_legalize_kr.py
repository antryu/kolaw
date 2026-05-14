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
from chromadb.api.types import Documents, EmbeddingFunction

from services.fast_search.ingest_legalize_kr import (
    _CORPUS_PATH,
    _law_docs,
    ingest_legalize_kr,
)

_CORPUS_AVAILABLE = _CORPUS_PATH.exists()
_TEST_LIMIT = 100  # process first 100 laws for test speed


class FakeEmbeddingFunction(EmbeddingFunction[Documents]):
    def __init__(self) -> None:
        pass

    @staticmethod
    def name() -> str:
        return "fake-test-embedding"

    @staticmethod
    def get_config() -> dict[str, str]:
        return {"name": "fake-test-embedding"}

    @classmethod
    def build_from_config(cls, config: dict[str, str]) -> "FakeEmbeddingFunction":
        return cls()

    def __call__(self, input: Documents) -> list[list[float]]:
        return [[float(len(text)), 1.0, 0.0] for text in input]


@pytest.mark.skipif(not _CORPUS_AVAILABLE, reason="legalize-kr corpus not mounted")
def test_ingest_100_laws_increases_count():
    """Ingesting 100 laws should add docs to ChromaDB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = ingest_legalize_kr(
            corpus_path=_CORPUS_PATH,
            persist_path=tmpdir,
            limit=_TEST_LIMIT,
            embedding_function=FakeEmbeddingFunction(),
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
            embedding_function=FakeEmbeddingFunction(),
        )
        first_ingested = result1["docs_ingested"]
        assert first_ingested > 0

        result2 = ingest_legalize_kr(
            corpus_path=_CORPUS_PATH,
            persist_path=tmpdir,
            limit=10,
            embedding_function=FakeEmbeddingFunction(),
        )
        assert result2["docs_ingested"] == 0, "Dedup failed: should skip all on second run"
        assert result2["docs_skipped"] == first_ingested


@pytest.mark.skipif(not _CORPUS_AVAILABLE, reason="legalize-kr corpus not mounted")
def test_ingest_chroma_count_matches():
    """ChromaDB collection count should reflect ingested docs."""
    import chromadb
    with tempfile.TemporaryDirectory() as tmpdir:
        result = ingest_legalize_kr(
            corpus_path=_CORPUS_PATH,
            persist_path=tmpdir,
            limit=5,
            embedding_function=FakeEmbeddingFunction(),
        )
        client = chromadb.PersistentClient(path=tmpdir)
        ef = FakeEmbeddingFunction()
        collection = client.get_collection(name="kolaw_laws", embedding_function=ef)
        assert collection.count() == result["docs_ingested"]


def test_ingest_missing_corpus_raises():
    """FileNotFoundError if corpus path does not exist."""
    with pytest.raises(FileNotFoundError, match="legalize-kr corpus not found"):
        ingest_legalize_kr(corpus_path=Path("/nonexistent/legalize-kr/kr"))


def test_law_docs_prepends_law_header_to_document():
    with tempfile.TemporaryDirectory() as tmpdir:
        law_dir = Path(tmpdir) / "근로기준법"
        law_dir.mkdir()
        (law_dir / "법률.md").write_text(
            """---
법령ID: 123456
제목: 근로기준법
시행일자: 2026-04-26
---
# 제1조(목적)
이 법은 근로조건의 기준을 정한다.
""",
            encoding="utf-8",
        )

        docs = list(_law_docs(law_dir))
        assert len(docs) == 1
        _, metadata, document = docs[0]
        assert metadata["law_name"] == "근로기준법"
        assert document.startswith("[근로기준법 제1조] ")
        assert "이 법은 근로조건의 기준을 정한다." in document


@pytest.mark.skipif(not _CORPUS_AVAILABLE, reason="legalize-kr corpus not mounted")
def test_ingest_metadata_fields():
    """Ingested docs should have required metadata fields."""
    import chromadb
    with tempfile.TemporaryDirectory() as tmpdir:
        ingest_legalize_kr(
            corpus_path=_CORPUS_PATH,
            persist_path=tmpdir,
            limit=3,
            embedding_function=FakeEmbeddingFunction(),
        )
        client = chromadb.PersistentClient(path=tmpdir)
        ef = FakeEmbeddingFunction()
        collection = client.get_collection(name="kolaw_laws", embedding_function=ef)
        results = collection.get(limit=5, include=["metadatas"])
        for meta in results["metadatas"]:
            assert "law_id" in meta
            assert "law_name" in meta
            assert "article" in meta
            assert "source_file" in meta
            assert "ingested_at" in meta
