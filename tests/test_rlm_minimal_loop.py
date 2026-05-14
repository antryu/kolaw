"""
test_rlm_minimal_loop.py — Phase 2 RLM minimal loop tests.

Tests the orchestrator run() function with:
- dry_run router (no real LLM call)
- fixture law loaded from legalize-kr (if available)
- degradation: RuntimeError → error response, no silent fallback
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from apps.api.schemas import SearchRequest
from services.rlm_engine.orchestrator import (
    TrajectoryLog,
    _extract_code,
    _trajectory_to_response,
    deep_search,
    run,
)


# ---- Unit: _extract_code ----

class TestExtractCode:
    def test_plain_code(self):
        code = "x = 1\nFINAL_ANSWER = x"
        assert _extract_code(code) == code

    def test_python_fenced(self):
        text = "```python\nx = 1\nFINAL_ANSWER = x\n```"
        assert _extract_code(text) == "x = 1\nFINAL_ANSWER = x"

    def test_generic_fenced(self):
        text = "```\nx = 1\n```"
        assert _extract_code(text) == "x = 1"

    def test_no_fence(self):
        text = "FINAL_ANSWER = []"
        assert _extract_code(text) == "FINAL_ANSWER = []"


# ---- Unit: _trajectory_to_response ----

class TestTrajectoryToResponse:
    def _make_req(self):
        return SearchRequest(query="수소", mode="deep")

    def test_error_response_returns_null_verdict(self):
        log = TrajectoryLog(trajectory_id=str(uuid.uuid4()), query="q", error="local_llm_unavailable")
        resp = _trajectory_to_response(log, self._make_req())
        assert resp.verdict is None
        assert resp.error == "local_llm_unavailable"
        assert resp.citations == []
        assert resp.trajectory_id is None

    def test_with_final_answer_list(self):
        log = TrajectoryLog(
            trajectory_id="abc-123",
            query="q",
            final_answer=[
                {
                    "law_id": "013670",
                    "law_name": "수소법",
                    "article": "제2조",
                    "excerpt": "수소충전소 관련",
                }
            ],
        )
        resp = _trajectory_to_response(log, self._make_req())
        assert resp.verdict == "applies"
        assert resp.confidence >= 0.5
        assert len(resp.citations) == 1
        assert resp.citations[0].law_id == "013670"
        assert resp.trajectory_id == "abc-123"
        assert resp.error is None

    def test_empty_final_answer(self):
        log = TrajectoryLog(
            trajectory_id="abc-456",
            query="q",
            final_answer=[],
        )
        resp = _trajectory_to_response(log, self._make_req())
        assert resp.verdict in ("does_not_apply", "ambiguous")
        assert resp.citations == []

    def test_none_final_answer(self):
        log = TrajectoryLog(trajectory_id="abc-789", query="q", final_answer=None)
        resp = _trajectory_to_response(log, self._make_req())
        assert resp.verdict == "ambiguous"


# ---- Integration: run() with mocked LLM ----

class TestRLMMinimalLoop:
    """Run the full RLM loop with a mocked LLM that returns valid code."""

    # Phase 3: sandbox uses RestrictedPython which forbids slice subscripts
    # (text[:100] etc). The mock builds FINAL_ANSWER without slicing on str.
    _MOCK_CODE = (
        "```python\n"
        "relevant = []\n"
        "for name in law_texts:\n"
        "    relevant.append({'law_id': 'mock001', 'law_name': name, "
        "'article': '제1조', 'excerpt': 'mock excerpt'})\n"
        "FINAL_ANSWER = relevant\n"
        "```"
    )

    @pytest.mark.asyncio
    async def test_run_sets_final_answer(self):
        """With mocked LLM and law_texts, FINAL_ANSWER should be set."""
        mock_complete = AsyncMock(return_value=self._MOCK_CODE)
        with patch("services.llm.router.complete", mock_complete):
            with patch(
                "services.fast_search.search._get_collection"
            ) as mock_col:
                mock_col.return_value.query.return_value = {
                    "documents": [["수소충전소 허가 관련 조문"]],
                    "metadatas": [[{"law_name": "수소법", "law_id": "013670"}]],
                    "distances": [[0.1]],
                }
                log = await run(query="수소충전소 허가 요건")

        assert log.final_answer is not None
        assert isinstance(log.final_answer, list)
        assert log.error is None
        # Phase 3 emits law_prefilter + llm_generate + exec → at least 3 steps.
        assert len(log.steps) >= 2

    @pytest.mark.asyncio
    async def test_run_llm_unavailable_returns_error(self):
        """RuntimeError from router → log.error = 'local_llm_unavailable', no silent fallback."""
        mock_complete = AsyncMock(
            side_effect=RuntimeError("Local LLM failed and ALLOW_ANTHROPIC is not set.")
        )
        with patch("services.llm.router.complete", mock_complete):
            with patch("services.fast_search.search._get_collection") as mock_col:
                mock_col.return_value.query.return_value = {
                    "documents": [[]],
                    "metadatas": [[]],
                    "distances": [[]],
                }
                log = await run(query="수소충전소")

        assert log.error == "local_llm_unavailable"
        assert log.final_answer is None

    @pytest.mark.asyncio
    async def test_run_retries_on_missing_final_answer(self):
        """If FINAL_ANSWER not set, router is called again (up to MAX_RETRIES)."""
        call_count = 0

        async def mock_complete_first_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                # First attempt: returns code without FINAL_ANSWER
                return "x = 1  # no FINAL_ANSWER"
            # Second attempt: sets FINAL_ANSWER
            return "FINAL_ANSWER = [{'law_id': 'x', 'law_name': 'y', 'article': 'z', 'excerpt': 'e'}]"

        with patch("services.llm.router.complete", mock_complete_first_fail):
            with patch("services.fast_search.search._get_collection") as mock_col:
                mock_col.return_value.query.return_value = {
                    "documents": [[]],
                    "metadatas": [[]],
                    "distances": [[]],
                }
                log = await run(query="test retry")

        assert call_count >= 2, "Should retry when FINAL_ANSWER not set"
        assert log.final_answer is not None

    @pytest.mark.asyncio
    async def test_run_with_explicit_laws(self):
        """Passing laws= skips fast_search and loads from legalize-kr."""
        mock_code = "FINAL_ANSWER = [{'law_id': 'L1', 'law_name': 'test', 'article': '제1조', 'excerpt': 'test'}]"
        mock_complete = AsyncMock(return_value=mock_code)

        with patch("services.llm.router.complete", mock_complete):
            with patch("services.data.legalize_kr.load_law") as mock_load:
                from services.data.legalize_kr import Article, ArticleTree
                mock_tree = ArticleTree(
                    law_id="L1",
                    law_name="Test Law",
                    version="20240101",
                    source_path="/fake/path",
                    articles=[Article(number="제1조", title="(목적)", content="이 법은 테스트를 위한 법이다.")],
                )
                mock_load.return_value = mock_tree
                log = await run(query="test", laws=["TestLaw"])

        assert log.final_answer is not None


# ---- Integration: deep_search degradation ----

class TestDeepSearchDegradation:
    @pytest.mark.asyncio
    async def test_deep_search_error_on_llm_unavailable(self):
        """deep_search() returns error='local_llm_unavailable' when LLM is down."""
        mock_complete = AsyncMock(
            side_effect=RuntimeError("Local LLM failed and ALLOW_ANTHROPIC is not set.")
        )
        with patch("services.llm.router.complete", mock_complete):
            with patch("services.fast_search.search._get_collection") as mock_col:
                mock_col.return_value.query.return_value = {
                    "documents": [[]],
                    "metadatas": [[]],
                    "distances": [[]],
                }
                req = SearchRequest(query="수소", mode="deep")
                result = await deep_search(req)

        assert result.error == "local_llm_unavailable"
        assert result.verdict is None
        assert result.citations == []

    @pytest.mark.asyncio
    async def test_deep_search_no_silent_fallback(self):
        """deep_search() MUST raise/return error, never silently return empty."""
        mock_complete = AsyncMock(
            side_effect=RuntimeError("Local LLM failed and ALLOW_ANTHROPIC is not set.")
        )
        with patch("services.llm.router.complete", mock_complete):
            with patch("services.fast_search.search._get_collection") as mock_col:
                mock_col.return_value.query.return_value = {
                    "documents": [[]],
                    "metadatas": [[]],
                    "distances": [[]],
                }
                req = SearchRequest(query="수소", mode="deep")
                result = await deep_search(req)

        # Must never silently return success
        assert result.error is not None, "Error must be surfaced, not silently swallowed"
