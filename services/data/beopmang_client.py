"""
법망(beopmang) API client.

Source: api.beopmang.org/api/v4/law
Provides structured metadata: article_count, case_count, xref_count, history_count.

Attribution: pattern adapted from antryu1b/hydrogen-law (law_api_client.py),
which uses the 국가법령정보센터 XML API with similar request/parse patterns.
This client targets the beopmang JSON API instead.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = os.getenv("BEOPMANG_BASE_URL", "https://api.beopmang.org/api/v4")
_TIMEOUT = 15.0


@dataclass
class LawMeta:
    law_id: str
    law_name: str
    law_type: str
    enforcement_date: str
    article_count: int
    case_count: int
    xref_count: int
    history_count: int


def _to_law_meta(raw: dict[str, Any]) -> LawMeta:
    return LawMeta(
        law_id=str(raw.get("law_id", "")),
        law_name=raw.get("law_name", ""),
        law_type=raw.get("law_type", ""),
        enforcement_date=raw.get("enforcement_date", ""),
        article_count=int(raw.get("article_count", 0)),
        case_count=int(raw.get("case_count", 0)),
        xref_count=int(raw.get("xref_count", 0)),
        history_count=int(raw.get("history_count", 0)),
    )


async def search(query: str, limit: int = 10) -> list[LawMeta]:
    """
    Search 법망 API for laws matching query.

    Returns list of LawMeta with case_count, xref_count etc.
    Returns empty list on any error (graceful degradation).
    """
    url = f"{_BASE_URL}/law"
    params = {"action": "search", "q": query, "mode": "keyword"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            laws = data if isinstance(data, list) else data.get("laws", [])
            return [_to_law_meta(item) for item in laws[:limit]]
    except Exception as exc:
        logger.warning("beopmang search failed for query=%r: %s", query, exc)
        return []


async def get_law(law_id: str) -> LawMeta | None:
    """
    Fetch a single law by law_id from 법망 API.
    """
    url = f"{_BASE_URL}/law"
    params = {"action": "get", "law_id": law_id}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "law_id" in data:
                return _to_law_meta(data)
            return None
    except Exception as exc:
        logger.warning("beopmang get_law failed for law_id=%r: %s", law_id, exc)
        return None
