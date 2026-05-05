import pytest
pytestmark = pytest.mark.skip(reason="Phase 1 ChromaDB / mock-RLM tests; superseded by Phase 3 architecture (services.data.legalize_kr.grep_search + services.data.law_go_kr.LawGoKrClient)")

"""
test_deep_mock.py — POST /search mode=deep returns valid trajectory_id + mock citations.
"""

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app)


def test_deep_mock_returns_200():
    resp = client.post(
        "/search",
        json={"query": "수소충전소 허가 요건", "mode": "deep"},
    )
    assert resp.status_code == 200


def test_deep_mock_has_trajectory_id():
    resp = client.post(
        "/search",
        json={"query": "수소충전소 허가 요건", "mode": "deep"},
    )
    body = resp.json()
    assert body["trajectory_id"] is not None
    assert len(body["trajectory_id"]) > 0
    assert body["mode"] == "deep"


def test_deep_mock_has_citations():
    resp = client.post(
        "/search",
        json={"query": "수소충전소 허가 요건", "mode": "deep"},
    )
    body = resp.json()
    assert len(body["citations"]) >= 1


def test_deep_mock_trajectory_id_unique():
    """Each call should return a distinct trajectory_id."""
    resp1 = client.post("/search", json={"query": "test", "mode": "deep"})
    resp2 = client.post("/search", json={"query": "test", "mode": "deep"})
    assert resp1.json()["trajectory_id"] != resp2.json()["trajectory_id"]
