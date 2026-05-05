"""
RLM Orchestrator — Phase 2 minimal loop.

run(query, laws=None) -> TrajectoryLog:
  1. Pre-filter: fast_search top-10 laws OR use caller-supplied laws list
  2. Load law text into REPL namespace
  3. Build system prompt + user query
  4. Call local LLM (router.complete) → generated Python code
  5. exec() code in RLMSession (sandboxed builtins)
  6. Capture FINAL_ANSWER variable
  7. Retry up to MAX_RETRIES on missing FINAL_ANSWER
  8. Return TrajectoryLog

Degradation (#3 resolved):
  - Local LLM failure → router.complete raises RuntimeError
  - Orchestrator catches it → returns {"verdict": null, "error": "local_llm_unavailable",
    "trajectory_id": null, "mode": "deep"} with HTTP 503
  - No silent fallback. Caller (Legaly agent) decides.

Phase 1 deep_search_mock remains as alias for API backward compat.

Reference: arXiv 2512.24601v2 (Recursive Language Models)
"""

from __future__ import annotations

import logging
import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from apps.api.schemas import Citation, SearchRequest, SearchResponse
from services.rlm_engine.repl import RLMSession

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

_SYSTEM_PROMPT = """You are a Korean legal research assistant operating in a REPL loop.

Two data sources are pre-loaded into your namespace. Either may be empty.

1) law_texts : dict[str, str]
   - law_name -> first ~20 articles from the offline legalize-kr corpus.
   - Empty if the corpus is not mounted on this machine.

2) live_results : dict[str, list[dict]]
   - Keys: "law", "precedent", "interpretation".
   - Each list item is {id, name, type, date, url} from law.go.kr.
   - Empty if LAW_GO_KR_OC is not set or the live fetch failed.

Your job: write Python that sets FINAL_ANSWER to a list of citation dicts.
Each citation MUST have: law_id, law_name, article, excerpt.

Picking source:
- Statute body / specific clause language  -> law_texts
- Case law (판례), interpretations         -> live_results["precedent"], live_results["interpretation"]
- Legal name / metadata only               -> live_results["law"]
- If both have signal, combine them.
- If both are empty: FINAL_ANSWER = []   (do NOT fabricate).

Example combining both sources:
```python
hits = []
for name, text in law_texts.items():
    if "허가" in text or "요건" in text:
        hits.append({"law_id": "legalize-kr", "law_name": name,
                     "article": "법률", "excerpt": text[:200]})
for it in live_results.get("precedent", []):
    hits.append({"law_id": it["id"], "law_name": it["name"],
                 "article": "판례", "excerpt": it.get("type", "")})
FINAL_ANSWER = hits[:5]
```

Write only valid Python. No imports. Set FINAL_ANSWER before the code ends."""


@dataclass
class TrajectoryLog:
    trajectory_id: str
    query: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_answer: Any = None
    error: str | None = None
    elapsed_ms: float = 0.0


async def run(
    query: str,
    laws: list[str] | None = None,
) -> TrajectoryLog:
    """
    Execute minimal RLM loop for a query.

    Args:
        query: Natural language legal question.
        laws: Optional list of law names (folder names from legalize-kr).
              If None, fast_search pre-filters top-10 relevant laws.

    Returns:
        TrajectoryLog with final_answer or error.
    """
    trajectory_id = str(uuid.uuid4())
    t0 = time.perf_counter()
    log = TrajectoryLog(trajectory_id=trajectory_id, query=query)

    # Step 1: resolve law texts
    law_texts: dict[str, str] = {}
    if laws:
        from services.data.legalize_kr import load_law

        for law_name in laws:
            tree = load_law(law_name)
            if tree:
                combined = "\n".join(
                    f"{a.number}{a.title}: {a.content[:500]}"
                    for a in tree.articles[:20]  # first 20 articles per law
                )
                law_texts[tree.law_name] = combined
        log.steps.append({"step": "law_load", "count": len(law_texts), "source": "caller"})
    else:
        # Pre-filter via grep_search over legalize-kr (offline). Skipped silently
        # if the corpus is not mounted — live_results below covers that case.
        try:
            from services.data.legalize_kr import grep_search, load_law

            grep_result = await grep_search(query, limit=8)
            for hit in grep_result.hits:
                tree = load_law(hit.law_name)
                if not tree or hit.law_name in law_texts:
                    continue
                combined = "\n".join(
                    f"{a.number}{a.title}: {a.content[:500]}"
                    for a in tree.articles[:20]
                )
                law_texts[hit.law_name] = combined
            log.steps.append(
                {"step": "law_prefilter", "count": len(law_texts),
                 "source": "grep_search", "mode": grep_result.mode}
            )
        except Exception as exc:
            logger.warning("grep_search prefilter failed (corpus missing?): %s", exc)
            log.steps.append({"step": "law_prefilter", "error": str(exc)})

    # Live results from law.go.kr Open API (Option C — hybrid). Fetched in parallel
    # so the LLM has access to fresh case law / interpretations even when the
    # offline corpus is missing or stale. Empty {} if OC not configured or
    # everything fails — the REPL prompt tells the LLM how to handle that.
    live_results: dict[str, list[dict]] = {"law": [], "precedent": [], "interpretation": []}
    try:
        if os.getenv("LAW_GO_KR_OC"):
            from services.data.law_go_kr import LawGoKrClient

            client = LawGoKrClient()
            law_items, prec_items, expc_items = await asyncio.gather(
                client.search_law(query, display=5),
                client.search_precedent(query, display=5),
                client.search_interpretation(query, display=3),
                return_exceptions=True,
            )

            def _to_dicts(items):
                if isinstance(items, BaseException) or not items:
                    return []
                return [
                    {
                        "id": it.item_id,
                        "name": it.title,
                        "type": it.subtitle,
                        "date": it.enforced_at,
                        "url": it.detail_url,
                    }
                    for it in items
                ]

            live_results = {
                "law": _to_dicts(law_items),
                "precedent": _to_dicts(prec_items),
                "interpretation": _to_dicts(expc_items),
            }
            log.steps.append(
                {
                    "step": "live_search",
                    "source": "law.go.kr",
                    "law": len(live_results["law"]),
                    "precedent": len(live_results["precedent"]),
                    "interpretation": len(live_results["interpretation"]),
                }
            )
        else:
            log.steps.append({"step": "live_search", "skipped": "LAW_GO_KR_OC not set"})
    except Exception as exc:
        logger.warning("law.go.kr live fetch failed: %s", exc)
        log.steps.append({"step": "live_search", "error": str(exc)})

    # Step 2: set up REPL session — both sources injected, either may be empty
    session = RLMSession()
    session.load("query", query)
    session.load("law_texts", law_texts)
    session.load("live_results", live_results)

    # Step 3-6: LLM → code → exec → FINAL_ANSWER
    from services.llm import router

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Query: {query}\n\n"
                f"Available laws: {list(law_texts.keys())}\n\n"
                "Write Python code to set FINAL_ANSWER."
            ),
        },
    ]

    final_answer = None
    for attempt in range(MAX_RETRIES):
        try:
            generated_code = await router.complete(messages, max_tokens=512, temperature=0.1)
        except RuntimeError as exc:
            # Local LLM unavailable — surface error, no silent fallback
            logger.error("LLM unavailable in RLM loop (attempt %d): %s", attempt + 1, exc)
            log.error = "local_llm_unavailable"
            log.elapsed_ms = (time.perf_counter() - t0) * 1000
            return log

        # Extract code block if wrapped in ```python ... ```
        code = _extract_code(generated_code)
        log.steps.append({"step": "llm_generate", "attempt": attempt + 1, "code_len": len(code)})

        exec_result = session.exec(code)
        log.steps.append({"step": "exec", "attempt": attempt + 1, "output": exec_result})

        final_answer = session.get("FINAL_ANSWER")
        if final_answer is not None:
            break

        # Feed back error for retry
        messages.append({"role": "assistant", "content": generated_code})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"FINAL_ANSWER was not set (exec result: {exec_result}). "
                    "Fix the code and try again. You MUST set FINAL_ANSWER."
                ),
            }
        )

    log.final_answer = final_answer
    log.elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info(
        "RLM trajectory=%s query=%r steps=%d final_answer_type=%s elapsed_ms=%.0f",
        trajectory_id,
        query,
        len(log.steps),
        type(final_answer).__name__,
        log.elapsed_ms,
    )
    return log


def _extract_code(text: str) -> str:
    """Extract Python code from ```python ... ``` block if present."""
    if "```python" in text:
        start = text.find("```python") + len("```python")
        end = text.find("```", start)
        if end > start:
            return text[start:end].strip()
    if "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end > start:
            return text[start:end].strip()
    return text.strip()


def _trajectory_to_response(
    log: TrajectoryLog, req: SearchRequest
) -> SearchResponse:
    """Convert TrajectoryLog to SearchResponse for the API."""
    if log.error:
        # Degradation: surface error, never silent fallback
        return SearchResponse(
            verdict=None,
            confidence=0.0,
            citations=[],
            trajectory_id=None,
            mode="deep",
            error=log.error,
        )

    citations: list[Citation] = []
    raw = log.final_answer
    if isinstance(raw, list):
        for item in raw[:10]:
            if isinstance(item, dict):
                citations.append(
                    Citation(
                        law_id=str(item.get("law_id", "unknown")),
                        law_name=item.get("law_name", ""),
                        article=item.get("article", ""),
                        version=item.get("version", ""),
                        excerpt=str(item.get("excerpt", ""))[:300],
                    )
                )

    verdict = "ambiguous"
    confidence = 0.5
    if citations:
        verdict = "applies"
        confidence = 0.7
    elif log.final_answer is not None:
        verdict = "does_not_apply"
        confidence = 0.3

    return SearchResponse(
        verdict=verdict,
        confidence=confidence,
        citations=citations,
        trajectory_id=log.trajectory_id,
        mode="deep",
    )


async def deep_search(req: SearchRequest) -> SearchResponse:
    """
    Phase 2 deep search via minimal RLM loop.
    On local LLM unavailable: returns 503-equivalent response with error field.
    """
    log = await run(query=req.query, laws=req.laws or None)
    return _trajectory_to_response(log, req)


async def deep_search_mock(req: SearchRequest) -> SearchResponse:
    """
    Phase 1 stub — preserved for backward compatibility with Phase 1 tests.

    Returns a fixed mock response with a real trajectory_id and mock citations.
    Phase 1 tests (test_deep_mock.py) assert trajectory_id is non-null and
    citations is non-empty. This stub satisfies both without an LLM call.

    The production /search endpoint uses deep_search() (real RLM loop).
    This stub is only retained so Phase 1 regression suite passes unchanged.
    """
    import uuid as _uuid

    trajectory_id = str(_uuid.uuid4())
    session = RLMSession()
    session.load("query", req.query)
    session.load("laws_filter", req.laws or [])

    mock_code = (
        "results = [{'law_id': '013670', 'article': '§2(7)', 'score': 0.91}]\n"
        "FINAL_ANSWER = results\n"
    )
    session.exec(mock_code)

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
