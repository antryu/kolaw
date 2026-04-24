"""
ChromaDB fast search path.

Handles 90% of queries via vector similarity on fixture data.
Returns structured Citations compatible with kolaw SearchResponse.
"""

from __future__ import annotations

import logging
import re

from apps.api.schemas import Citation, SearchRequest, SearchResponse
from services.fast_search.ingest import (
    _COLLECTION_NAME,
    get_chroma_client,
    get_embedding_function,
    ingest,
)

logger = logging.getLogger(__name__)

_TOP_K = 5
_COLLECTION_CACHE: dict = {}


def _get_collection():
    client = get_chroma_client()
    ef = get_embedding_function()
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef,
    )
    # Auto-ingest fixture if collection is empty
    if collection.count() == 0:
        logger.info("Collection empty — auto-ingesting fixture data")
        ingest()
    return collection


def _meta_to_citation(doc: str, meta: dict, distance: float) -> Citation:
    """Convert a ChromaDB result row to a Citation."""
    law_id = meta.get("law_id", "unknown")
    law_name = meta.get("law_name", "")
    article_number = meta.get("article_number", "")
    enforcement_date = meta.get("enforcement_date", "")

    # Format article as §N format
    match = re.search(r"(\d+)", article_number)
    article_ref = f"§{match.group(1)}" if match else article_number

    excerpt = doc[:200].replace("\n", " ") if doc else ""
    confidence_from_distance = max(0.0, 1.0 - distance)

    return Citation(
        law_id=law_id,
        law_name=law_name,
        article=article_ref,
        version=enforcement_date.replace("-", "") if enforcement_date else "",
        excerpt=excerpt,
    )


async def fast_search(req: SearchRequest) -> SearchResponse:
    """
    Vector search via ChromaDB.
    Returns SearchResponse with citations ranked by similarity.
    """
    try:
        collection = _get_collection()
        results = collection.query(
            query_texts=[req.query],
            n_results=min(_TOP_K, collection.count()),
        )
    except Exception as exc:
        logger.error("ChromaDB query failed: %s", exc)
        return SearchResponse(
            verdict="ambiguous",
            confidence=0.0,
            citations=[],
            trajectory_id=None,
            mode="fast",
        )

    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    citations = []
    for doc, meta, dist in zip(documents, metadatas, distances):
        citations.append(_meta_to_citation(doc, meta or {}, dist))

    # Confidence: mean similarity of top results
    confidence = 0.0
    if distances:
        confidence = round(max(0.0, 1.0 - min(distances)), 3)

    verdict = None
    if confidence >= 0.7:
        verdict = "applies"
    elif confidence >= 0.4:
        verdict = "ambiguous"
    else:
        verdict = "does_not_apply"

    return SearchResponse(
        verdict=verdict,
        confidence=confidence,
        citations=citations,
        trajectory_id=None,
        mode="fast",
    )
