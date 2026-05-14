"""
test_deep_mock.py — POST /search mode=deep returns valid trajectory_id + citations.

Phase 3: /search?mode=deep is wired to the real RLM loop (deep_search), so
these integration tests mock the LLM router + ChromaDB to keep the assertions
deterministic without spinning up llama-swap.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app)


_MOCK_DEEP_CODE = (
    "FINAL_ANSWER = [{'law_id': '013670', 'law_name': '수소법', "
    "'article': '제2조', 'excerpt': 'mock excerpt'}]"
)


def _mock_chroma_query(*_args, **_kwargs):
    return {
        "documents": [["수소충전소 허가 관련 조문"]],
        "metadatas": [[{"law_name": "수소법", "law_id": "013670"}]],
        "distances": [[0.1]],
    }


def _patched_deep_call(query: str = "수소충전소 허가 요건"):
    """Helper: invoke /search?mode=deep with router + chromadb mocks."""
    mock_complete = AsyncMock(return_value=_MOCK_DEEP_CODE)
    with patch("services.llm.router.complete", mock_complete):
        with patch("services.fast_search.search._get_collection") as mock_col:
            mock_col.return_value.query.side_effect = _mock_chroma_query
            return client.post("/search", json={"query": query, "mode": "deep"})


def test_deep_mock_returns_200():
    resp = _patched_deep_call()
    assert resp.status_code == 200


def test_deep_mock_has_trajectory_id():
    resp = _patched_deep_call()
    body = resp.json()
    assert body["trajectory_id"] is not None
    assert len(body["trajectory_id"]) > 0
    assert body["mode"] == "deep"


def test_deep_mock_has_citations():
    resp = _patched_deep_call()
    body = resp.json()
    assert len(body["citations"]) >= 1


def test_deep_mock_trajectory_id_unique():
    """Each call should return a distinct trajectory_id."""
    resp1 = _patched_deep_call("test1")
    resp2 = _patched_deep_call("test2")
    assert resp1.json()["trajectory_id"] != resp2.json()["trajectory_id"]
