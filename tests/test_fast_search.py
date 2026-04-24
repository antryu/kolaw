"""
test_fast_search.py — POST /search mode=fast on 수소법 query returns >= 1 citation.
"""

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app)


def test_fast_search_returns_200():
    resp = client.post(
        "/search",
        json={"query": "수소충전소 허가 요건", "mode": "fast"},
    )
    assert resp.status_code == 200


def test_fast_search_schema():
    resp = client.post(
        "/search",
        json={"query": "수소충전소 허가 요건", "mode": "fast"},
    )
    body = resp.json()
    assert body["mode"] == "fast"
    assert "confidence" in body
    assert isinstance(body["confidence"], float)
    assert "citations" in body
    assert isinstance(body["citations"], list)
    assert body["trajectory_id"] is None


def test_fast_search_returns_citations():
    resp = client.post(
        "/search",
        json={"query": "수소충전소 허가 요건", "mode": "fast"},
    )
    body = resp.json()
    assert len(body["citations"]) >= 1, "Expected at least 1 citation from fixture data"


def test_fast_search_citation_schema():
    resp = client.post(
        "/search",
        json={"query": "수소 안전관리", "mode": "fast"},
    )
    body = resp.json()
    if body["citations"]:
        c = body["citations"][0]
        assert "law_id" in c
        assert "law_name" in c
        assert "article" in c
        assert "version" in c
        assert "excerpt" in c
