"""
test_article.py — GET /article deterministic per-article lookup.

Verifies the article-lookup endpoint returns verbatim 제N조 text from the
legalize-kr corpus, including 의-articles, with clean 404s on missing input.
"""

from fastapi.testclient import TestClient

from apps.api.main import app

client = TestClient(app)


def test_article_plain_found():
    """개인정보보호법 제15조 — verbatim text with all sub-items."""
    resp = client.get("/article", params={"law": "개인정보보호법", "article": "제15조"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["article"] == "제15조"
    assert body["law_id"] == "011357"
    assert "개인정보의 수집" in body["title"]
    # 제1항 7개 호 + 제2항 + 제3항 본문이 모두 들어야 한다.
    assert "정보주체의 동의를 받은 경우" in body["text"]
    assert "공중위생 등 공공의 안전" in body["text"]
    assert body["text"].count("**①**") == 1
    assert body["provenance"] == "legalize-kr-file"
    assert body["source_path"].endswith("법률.md")


def test_article_eui_found():
    """제7조의2 — 의-article recovered positionally despite stripped suffix."""
    resp = client.get("/article", params={"law": "개인정보보호법", "article": "제7조의2"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["article"] == "제7조의2"
    assert body["text"]  # non-empty verbatim body


def test_article_spaced_law_name():
    """법령명에 공백이 있어도 폴더를 찾아야 한다 (개인정보 보호법 == 개인정보보호법)."""
    resp = client.get("/article", params={"law": "개인정보 보호법", "article": "제15조"})
    assert resp.status_code == 200
    assert resp.json()["found"] is True


def test_article_sirhaengnyeong_type():
    """type=시행령 으로 시행령 조문을 조회할 수 있어야 한다."""
    resp = client.get(
        "/article",
        params={"law": "개인정보보호법", "article": "제14조의2", "type": "시행령"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["type"] == "시행령"


def test_article_missing_law_404():
    resp = client.get("/article", params={"law": "존재안하는법", "article": "제1조"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["found"] is False
    assert body["error"]


def test_article_missing_article_404():
    resp = client.get("/article", params={"law": "개인정보보호법", "article": "제9999조"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["found"] is False
    assert body["error"]
