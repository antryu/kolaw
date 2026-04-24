"""
RLM Orchestrator — Phase 1 stub.

Takes a query, constructs system prompt, runs code in RLMSession,
captures FINAL_ANSWER, returns trajectory log.

Phase 1: returns mock with a real trajectory_id UUID.
Phase 2: wire to actual LLM via services/llm/router.py + multi-turn loop.

Reference: arXiv 2512.24601v2 (Recursive Language Models)
"""

from __future__ import annotations

import logging
import time
import uuid

from apps.api.schemas import Citation, SearchRequest, SearchResponse
from services.rlm_engine.repl import RLMSession

logger = logging.getLogger(__name__)


async def deep_search_mock(req: SearchRequest) -> SearchResponse:
    """
    Phase 1 deep mode: proves the trajectory loop structure works.
    Returns a valid SearchResponse with a real trajectory_id and mock citations.

    Phase 2: replace the mock LLM call with services.llm.router.complete().
    """
    trajectory_id = str(uuid.uuid4())

    # Demonstrate the RLM session is wired
    session = RLMSession()
    session.load("query", req.query)
    session.load("laws_filter", req.laws or [])

    # Phase 1: mock "LLM writes code" step
    mock_code = (
        "# Phase 1 mock — LLM would write search code here\n"
        "results = [{'law_id': '013670', 'article': '§2(7)', 'score': 0.91}]\n"
        "FINAL_ANSWER = results\n"
    )
    session.exec(mock_code)
    final_answer = session.get("FINAL_ANSWER")

    logger.info(
        "RLM trajectory=%s query=%r steps=%d final_answer=%r",
        trajectory_id,
        req.query,
        len(session.history),
        final_answer,
    )

    mock_citations = [
        Citation(
            law_id="013670",
            law_name="수소경제 육성 및 수소 안전관리에 관한 법률",
            article="§2(7)",
            version="20251001",
            excerpt="[Phase 1 mock] RLM deep search — wire real LLM in Phase 2",
        )
    ]

    return SearchResponse(
        verdict="ambiguous",
        confidence=0.5,
        citations=mock_citations,
        trajectory_id=trajectory_id,
        mode="deep",
    )
