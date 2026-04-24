"""
법망(beopmang) API client.

Source: api.beopmang.org/api/v4/law
Provides structured metadata: article_count, case_count, matched_articles.

Phase 2 fix: API wraps all responses in {"data": {...}, "meta": {...}}.
The original client parsed top-level keys causing empty responses.

Investigation outcome (2026-04-24):
- API returns HTTP 200 with valid JSON always
- Wrapper: response["data"]["results"] for search, response["data"] for get
- search result fields: law_id, law_name, law_type, matched_articles, score
- get result fields: law_id, law_name, law_name_short, law_type, article_count, top_articles
- No auth required, no IP whitelist — fully public API
- beopmang outcome: (a) resolved — schema mismatch was root cause
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = os.getenv("BEOPMANG_BASE_URL", "https://api.beopmang.org/api/v4")
_TIMEOUT = 15.0


@dataclass
class MatchedArticle:
    label: str
    snippet: str


@dataclass
class LawMeta:
    law_id: str
    law_name: str
    law_type: str
    # search results have score + matched_articles
    score: int = 0
    matched_articles: list[MatchedArticle] = field(default_factory=list)
    # get results have article_count
    article_count: int = 0


def _parse_matched_articles(raw: list[dict[str, Any]]) -> list[MatchedArticle]:
    return [MatchedArticle(label=a.get("label", ""), snippet=a.get("snippet", "")) for a in raw]


def _to_law_meta_from_search(raw: dict[str, Any]) -> LawMeta:
    return LawMeta(
        law_id=str(raw.get("law_id", "")),
        law_name=raw.get("law_name", ""),
        law_type=raw.get("law_type", ""),
        score=int(raw.get("score", 0)),
        matched_articles=_parse_matched_articles(raw.get("matched_articles", [])),
    )


def _to_law_meta_from_get(raw: dict[str, Any]) -> LawMeta:
    return LawMeta(
        law_id=str(raw.get("law_id", "")),
        law_name=raw.get("law_name", ""),
        law_type=raw.get("law_type", ""),
        article_count=int(raw.get("article_count", 0)),
    )


async def search(query: str, limit: int = 10) -> list[LawMeta]:
    """
    Search 법망 API for laws matching query.

    Response: {"data": {"results": [...], "total": N, "mode": "..."}, "meta": {...}}
    Returns list of LawMeta. Raises on HTTP error.
    """
    url = f"{_BASE_URL}/law"
    params = {"action": "search", "q": query, "mode": "keyword"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        envelope = resp.json()
        # API wraps results in "data" key
        data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
        results = data.get("results", []) if isinstance(data, dict) else []
        return [_to_law_meta_from_search(item) for item in results[:limit]]


async def get_law(law_id: str) -> LawMeta | None:
    """
    Fetch a single law by law_id from 법망 API.

    Response: {"data": {"law_id": ..., "article_count": ..., ...}, "meta": {...}}
    """
    url = f"{_BASE_URL}/law"
    params = {"action": "get", "law_id": law_id}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        envelope = resp.json()
        data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
        if isinstance(data, dict) and "law_id" in data:
            return _to_law_meta_from_get(data)
        return None
