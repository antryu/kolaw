"""
ChromaDB ingest script for Phase 1 fixture data.

Source: ~/PRJs/hydrogen-law/services/rag-engine/law_documents.json
Uses first 5 documents from that file as the Phase 1 fixture.

Embeddings: BAAI/bge-m3 (1024-dim multilingual)
Collection: KOLAW_COLLECTION env override, default "kolaw_laws_v2"

Run: python -m services.fast_search.ingest
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_FIXTURE_PATH = Path(
    os.getenv(
        "FIXTURE_JSON",
        str(Path.home() / "PRJs/hydrogen-law/services/rag-engine/law_documents.json"),
    )
)
_COLLECTION_NAME = os.getenv("KOLAW_COLLECTION", "kolaw_laws")
_EMBEDDING_MODEL = os.getenv("KOLAW_EMBEDDING_MODEL", "jhgan/ko-sroberta-multitask")
_CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
_CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
_FIXTURE_LIMIT = int(os.getenv("FIXTURE_LIMIT", "5"))


def get_chroma_client():
    import chromadb

    # In Docker: connect to chromadb container
    # Locally: use ephemeral in-memory client for tests
    chroma_path = os.getenv("CHROMA_PERSIST_PATH", "")
    if chroma_path:
        return chromadb.PersistentClient(path=chroma_path)
    try:
        client = chromadb.HttpClient(host=_CHROMA_HOST, port=_CHROMA_PORT)
        client.heartbeat()
        return client
    except Exception:
        logger.info("ChromaDB HTTP not reachable, using ephemeral client")
        return chromadb.EphemeralClient()


def get_embedding_function():
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    # Force MPS (Apple Silicon GPU) when available; falls back to CPU otherwise.
    # Default device=None lets sentence-transformers pick CPU on macOS — too slow
    # for full re-ingest of 130K chunks (43+ hr CPU vs ~2 hr MPS for bge-m3).
    device = os.getenv("KOLAW_EMBEDDING_DEVICE")
    if not device:
        try:
            import torch
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                device = "mps"
            elif torch.cuda.is_available():
                device = "cuda"
            else:
                device = "cpu"
        except Exception:
            device = "cpu"
    return SentenceTransformerEmbeddingFunction(
        model_name=_EMBEDDING_MODEL,
        device=device,
    )


def ingest(limit: int = _FIXTURE_LIMIT) -> int:
    """
    Ingest up to `limit` documents from the fixture JSON into ChromaDB.
    Returns number of documents ingested.
    """
    if not _FIXTURE_PATH.exists():
        raise FileNotFoundError(f"Fixture not found: {_FIXTURE_PATH}")

    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        docs = json.load(f)

    sample = docs[:limit]

    client = get_chroma_client()
    ef = get_embedding_function()
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef,
    )

    ids = [d["id"] for d in sample]
    contents = [d["content"] for d in sample]
    metadatas = [d.get("metadata", {}) for d in sample]

    existing = set(collection.get(ids=ids)["ids"])
    new_ids = [i for i in ids if i not in existing]
    new_contents = [c for i, c in zip(ids, contents) if i not in existing]
    new_metadatas = [m for i, m in zip(ids, metadatas) if i not in existing]

    if new_ids:
        collection.add(ids=new_ids, documents=new_contents, metadatas=new_metadatas)
        logger.info("Ingested %d new documents into collection '%s'", len(new_ids), _COLLECTION_NAME)
    else:
        logger.info("All %d fixture documents already in collection", len(sample))

    return len(new_ids)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    count = ingest()
    print(f"Ingested {count} documents")
