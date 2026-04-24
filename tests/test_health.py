"""
test_health.py — GET /health returns 200 with expected shape.
"""

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app)


def test_health_200():
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_shape():
    resp = client.get("/health")
    body = resp.json()
    assert "status" in body
    assert body["status"] in ("ok", "degraded")
    assert "version" in body
    assert "data_sources" in body
    assert isinstance(body["data_sources"], list)
    assert len(body["data_sources"]) >= 3


def test_health_data_source_names():
    resp = client.get("/health")
    names = [s["name"] for s in resp.json()["data_sources"]]
    assert "legalize-kr" in names
    assert "beopmang" in names
    assert "korean-law-mcp" in names
