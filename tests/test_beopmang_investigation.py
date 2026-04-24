"""
test_beopmang_investigation.py — Phase 2 beopmang API investigation tests.

Investigation outcome (2026-04-24): (a) RESOLVED
Root cause: API wraps all responses in {"data": {...}, "meta": {...}}.
  - search: response["data"]["results"] (list of law matches)
  - get: response["data"] (single law object with law_id, article_count, etc.)
  - No auth required, no IP restriction — fully public API
  - HTTP 200 with application/json Content-Type always returned

Tests verify the fixed client parses the envelope correctly.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.data.beopmang_client import LawMeta, MatchedArticle, get_law, search


# --- Mock helpers ---

def _search_envelope(results: list) -> dict:
    return {"data": {"total": len(results), "results": results, "mode": "keyword"}, "meta": {}}


def _get_envelope(law_data: dict | None) -> dict:
    if law_data is None:
        return {"data": {}, "meta": {}}
    return {"data": law_data, "meta": {}}


class TestBeopmangSearchFixed:
    """Verify fixed search() parses envelope correctly (mocked HTTP)."""

    @pytest.mark.asyncio
    async def test_search_returns_list(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = _search_envelope([
            {"law_id": "001", "law_name": "수소법", "law_type": "법률", "matched_articles": [], "score": 10}
        ])
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await search("수소", limit=5)

        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0].law_id == "001"

    @pytest.mark.asyncio
    async def test_search_result_has_law_id(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = _search_envelope([
            {
                "law_id": "013670",
                "law_name": "수소경제 육성 및 수소 안전관리에 관한 법률",
                "law_type": "법률",
                "matched_articles": [{"label": "제2조", "snippet": "수소충전소 정의"}],
                "score": 95,
            }
        ])
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await search("수소", limit=3)

        assert results[0].law_id == "013670"
        assert results[0].law_name != ""

    @pytest.mark.asyncio
    async def test_search_result_has_matched_articles(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = _search_envelope([
            {
                "law_id": "013670",
                "law_name": "수소법",
                "law_type": "법률",
                "matched_articles": [
                    {"label": "제2조", "snippet": "수소충전소"},
                    {"label": "제15조", "snippet": "허가"},
                ],
                "score": 80,
            }
        ])
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await search("수소충전소", limit=3)

        assert len(results[0].matched_articles) == 2
        assert isinstance(results[0].matched_articles[0], MatchedArticle)

    @pytest.mark.asyncio
    async def test_search_empty_envelope_returns_empty_list(self):
        """Empty results list from API should return empty list."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _search_envelope([])
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await search("zzzzunlikely_query_zzz", limit=5)

        assert results == []

    @pytest.mark.asyncio
    async def test_old_flat_schema_returns_empty(self):
        """
        Root cause: Phase 1 client used top-level keys.
        Old schema: {"law_id": ..., "laws": [...]} → now wrapped in data key.
        Fixed client: returns empty list for old flat schema (no data key).
        """
        mock_resp = MagicMock()
        # Old (incorrect) schema that Phase 1 client expected
        mock_resp.json.return_value = {"laws": [{"law_id": "x"}]}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await search("수소", limit=5)

        # Fixed client correctly returns empty — old schema has no "data" key
        assert results == [], "Fixed client should return [] for legacy flat schema"


class TestBeopmangGetFixed:
    """Verify fixed get_law() parses envelope correctly (mocked HTTP)."""

    @pytest.mark.asyncio
    async def test_get_known_law(self):
        """get_law() should parse data envelope and return LawMeta."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _get_envelope({
            "law_id": "013670",
            "law_name": "수소경제 육성 및 수소 안전관리에 관한 법률",
            "law_type": "법률",
            "article_count": 68,
        })
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(return_value=mock_resp)
            result = await get_law("013670")

        assert result is not None
        assert result.law_id == "013670"
        assert "수소" in result.law_name
        assert result.article_count == 68

    @pytest.mark.asyncio
    async def test_get_missing_law_id_returns_none(self):
        """Response with no law_id in data should return None."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"error": "not found"}, "meta": {}}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(return_value=mock_resp)
            result = await get_law("XXXXINVALID")

        assert result is None


class TestBeopmangSchemaInvestigation:
    """Document the API schema for Phase 3 reference."""

    @pytest.mark.asyncio
    async def test_response_wraps_in_data_key(self):
        """
        Root cause of Phase 1 empty response: API wraps in data key.
        Fixed client now unwraps correctly.
        Investigation result: beopmang outcome (a) — resolved.
        """
        mock_resp = MagicMock()
        mock_resp.json.return_value = _search_envelope([
            {"law_id": "m001", "law_name": "민법", "law_type": "법률", "matched_articles": [], "score": 5}
        ])
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(return_value=mock_resp)
            results = await search("민법", limit=2)

        assert isinstance(results, list)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_no_auth_header_sent(self):
        """Client sends no Authorization header (API is public)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _search_envelope([])
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(return_value=mock_resp)
            await search("형법", limit=1)

        call_kwargs = mock_client.get.call_args
        # No auth header passed — params only
        params = call_kwargs.kwargs.get("params", {})
        assert "Authorization" not in str(call_kwargs)
