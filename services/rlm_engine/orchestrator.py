"""
RLM Orchestrator — Phase 3 (Trajectory + Budget + sub_llm).

Phase 3 flow per call:
  1. Trajectory.new() + TokenBudget()
  2. fast_search prefilter (or caller-supplied laws) → top 5–10 candidates
  3. Load law text via legalize_kr.load_law() → law_texts dict
     {law_name: {article_number: content}}
  4. RLMSession.load("query", query) + load("law_texts", law_texts)
  5. inject_callable("sub_llm", make_sub_llm(...)) — depth=1 child for sub-LLM
  6. router.complete(messages) for root; record root_completion event
  7. session.exec(root_code) — sub_llm calls self-record on trajectory
  8. session.get("FINAL_ANSWER"); retry up to MAX_RETRIES if missing
  9. Record final_answer event; set elapsed_ms
 10. KOLAW_PERSIST_TRAJECTORY=1 → traj.persist()
 11. Build SearchResponse with trajectory_id

Errors → SearchResponse.error one of:
  - "budget_exceeded"          (BudgetExceeded raised)
  - "recursion_depth_exceeded" (RecursionDepthExceeded raised)
  - "local_llm_unavailable"    (router RuntimeError)
  - "rlm_error"                (other unexpected exception)

Phase 1 deep_search_mock is preserved for backward compat (unused by API
unless ?compat=mock is wired).

Reference: arXiv 2512.24601v2 (Recursive Language Models).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.api.schemas import Citation, SearchRequest, SearchResponse
from services.rlm_engine.budget import (
    BudgetExceeded,
    RecursionDepthExceeded,
    TokenBudget,
)
from services.rlm_engine.repl import RLMSession
from services.rlm_engine.sub_llm import make_sub_llm
from services.rlm_engine.trajectory import Trajectory, TrajectoryEvent

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
_PREFILTER_TOP_K = 8
_ARTICLES_PER_LAW = 12  # cap per law to keep prompt + budget bounded

# Phase 3 system prompt — instructs root LLM how to use sub_llm + REPL.
_SYSTEM_PROMPT_V3 = """You are a Korean legal research engine using RLM (Recursive Language Models).

You have a Python REPL with these variables already bound:
- query: str — the user's question
- law_texts: dict[str, dict[str, str]] — {law_name: {article_number: content}}
- sub_llm(prompt, *, max_tokens=512, temperature=0.1) -> str — call a sub-LLM for focused analysis

Your job:
1. Examine `query` and `law_texts`.
2. For each candidate law, decide if you need a sub_llm call to extract relevant articles.
3. Sub-LLM calls return strings. If you get "[sub_llm_error] ..." treat it as a soft failure.
4. Build the final result as a list of dicts in variable FINAL_ANSWER:
   FINAL_ANSWER = [
     {"law_id": "...", "law_name": "...", "article": "제N조", "excerpt": "..."},
     ...
   ]

Constraints:
- No imports, no file IO, no eval/exec, no dunder access.
- Max sub_llm depth: 3.
- Output Python code only — no explanation. The code will be exec()'d.

IMPORTANT: string slice subscripts (e.g. text[:200]) are REJECTED by the
sandbox (RestrictedPython does not allow slice keys on strings). Use these
non-slice idioms instead:
  - first sentence: text.partition(". ")[0]
  - first line:     text.splitlines()[0] if text.splitlines() else ""
  - first chunk:    text.partition(chr(10))[0]
List slicing (e.g. items[:5]) IS allowed.

Example pattern (do not copy verbatim):
candidates = []
for law_name, articles in law_texts.items():
    if "수소" in law_name or any("수소" in a for a in articles.values()):
        items = list(articles.items())[:5]                     # list slice OK
        joined = " | ".join(f"{n}: {c.partition(chr(10))[0]}" for n, c in items)
        summary = sub_llm("From " + law_name + " articles, list ones relevant to: " + query
                          + "\\n\\n" + joined, max_tokens=400)
        excerpt = summary.partition("\\n\\n")[0]               # no string slice
        candidates.append({"law_id": "unknown", "law_name": law_name,
                           "article": "제1조", "excerpt": excerpt})
FINAL_ANSWER = candidates[:5]                                  # list slice OK
"""


@dataclass
class TrajectoryLog:
    """Lightweight log preserved for Phase 2 callers (test_rlm_minimal_loop)."""

    trajectory_id: str
    query: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_answer: Any = None
    error: str | None = None
    elapsed_ms: float = 0.0


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


def _resolve_law_texts(
    query: str,
    laws: list[str] | None,
    log: TrajectoryLog,
    traj: Trajectory,
) -> dict[str, dict[str, str]]:
    """
    Build {law_name: {article_number: content}}.

    Priority:
      - explicit `laws` list → load via legalize_kr.load_law()
      - else: fast_search prefilter (ChromaDB) → load top-K via load_law()
      - fallback (no fast_search hit): single doc text from prefilter, keyed
        as "전문" (full text) so the root LLM still sees something.
    """
    law_texts: dict[str, dict[str, str]] = {}

    # --- explicit laws list ---
    if laws:
        from services.data.legalize_kr import load_law

        for law_name in laws:
            tree = load_law(law_name)
            if not tree:
                continue
            articles = {
                a.number: (a.content[:1000])
                for a in tree.articles[:_ARTICLES_PER_LAW]
            }
            law_texts[tree.law_name] = articles
            traj.append(
                TrajectoryEvent.new(
                    kind="law_load",
                    depth=0,
                    payload={
                        "source": "caller",
                        "law_name": tree.law_name,
                        "law_id": tree.law_id,
                        "articles": len(articles),
                    },
                )
            )
        log.steps.append(
            {"step": "law_load", "count": len(law_texts), "source": "caller"}
        )
        return law_texts

    # --- fast_search prefilter ---
    try:
        from services.data.legalize_kr import load_law
        from services.fast_search.search import _get_collection

        collection = _get_collection()
        results = collection.query(query_texts=[query], n_results=_PREFILTER_TOP_K)
        metas = results.get("metadatas", [[]])[0]
        docs = results.get("documents", [[]])[0]

        # Dedupe by law_name; pick first hit per law to drive load_law().
        seen: set[str] = set()
        for meta, doc in zip(metas, docs):
            if not meta:
                continue
            display_name = meta.get("law_name", "")
            folder_name = meta.get("law_folder") or display_name
            if not display_name or display_name in seen:
                # No metadata law_name: fall back to inline doc.
                if not display_name:
                    inline_key = "unnamed_" + str(len(law_texts))
                    if inline_key not in law_texts:
                        law_texts[inline_key] = {"전문": (doc or "")[:1000]}
                continue
            seen.add(display_name)
            tree = load_law(folder_name) if folder_name else None
            if tree:
                articles = {
                    a.number: (a.content[:1000])
                    for a in tree.articles[:_ARTICLES_PER_LAW]
                }
                law_texts[tree.law_name] = articles
                traj.append(
                    TrajectoryEvent.new(
                        kind="law_load",
                        depth=0,
                        payload={
                            "source": "fast_search",
                            "law_name": tree.law_name,
                            "law_id": tree.law_id,
                            "articles": len(articles),
                        },
                    )
                )
            else:
                # legalize_kr couldn't resolve folder — use the chroma doc snippet.
                law_texts[display_name] = {"전문": (doc or "")[:1000]}
                traj.append(
                    TrajectoryEvent.new(
                        kind="law_load",
                        depth=0,
                        payload={
                            "source": "fast_search_inline",
                            "law_name": display_name,
                            "articles": 1,
                        },
                    )
                )

        log.steps.append(
            {"step": "law_prefilter", "count": len(law_texts), "source": "fast_search"}
        )
    except Exception as exc:  # noqa: BLE001 — prefilter is best-effort
        logger.warning("fast_search prefilter failed: %s", exc)
        log.steps.append({"step": "law_prefilter", "error": str(exc)})

    return law_texts


async def run(
    query: str,
    laws: list[str] | None = None,
) -> TrajectoryLog:
    """
    Execute the Phase 3 RLM loop. Returns a TrajectoryLog (legacy shape) so
    Phase 2 tests keep working; rich audit lives on a side Trajectory which
    `deep_search()` persists when KOLAW_PERSIST_TRAJECTORY=1.
    """
    trajectory_id = str(uuid.uuid4())
    t0 = time.perf_counter()
    log = TrajectoryLog(trajectory_id=trajectory_id, query=query)

    traj = Trajectory.new(query=query)
    # Use the same id on both records so callers can correlate.
    traj.trajectory_id = trajectory_id
    budget = TokenBudget()

    # 1+2: resolve laws → law_texts
    law_texts = _resolve_law_texts(query, laws, log, traj)

    # 3: REPL session + injected sub_llm
    session = RLMSession()
    session.load("query", query)
    session.load("law_texts", law_texts)

    # Root prompt event (recorded BEFORE LLM call for accurate parent linkage).
    root_prompt_event = TrajectoryEvent.new(
        kind="root_prompt",
        depth=0,
        payload={
            "query": query,
            "law_count": len(law_texts),
            "system_prompt_chars": len(_SYSTEM_PROMPT_V3),
        },
    )
    traj.append(root_prompt_event)

    # 4: LLM call(s) — retry on missing FINAL_ANSWER
    from services.llm import router

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT_V3},
        {
            "role": "user",
            "content": (
                f"Query: {query}\n\n"
                f"Available laws ({len(law_texts)}): "
                f"{list(law_texts.keys())}\n\n"
                "Write Python code that sets FINAL_ANSWER (no explanation)."
            ),
        },
    ]

    final_answer: Any = None
    last_root_completion_event: TrajectoryEvent | None = None
    last_exec_error: str | None = None

    for attempt in range(MAX_RETRIES):
        # --- root LLM call ---
        try:
            generated_code = await router.complete(
                messages, max_tokens=1024, temperature=0.1
            )
        except RuntimeError as exc:
            logger.error(
                "LLM unavailable (attempt %d/%d): %s",
                attempt + 1,
                MAX_RETRIES,
                exc,
            )
            log.error = "local_llm_unavailable"
            log.elapsed_ms = (time.perf_counter() - t0) * 1000
            traj.error = "local_llm_unavailable"
            traj.elapsed_ms = log.elapsed_ms
            _maybe_persist(traj)
            return log
        except Exception as exc:  # noqa: BLE001 — surface broad failures cleanly
            logger.exception("Unexpected router error: %s", exc)
            log.error = "rlm_error"
            log.elapsed_ms = (time.perf_counter() - t0) * 1000
            traj.error = f"rlm_error: {type(exc).__name__}: {str(exc)[:200]}"
            traj.elapsed_ms = log.elapsed_ms
            _maybe_persist(traj)
            return log

        code = _extract_code(generated_code)
        completion_event = TrajectoryEvent.new(
            kind="root_completion",
            depth=0,
            parent_event_id=root_prompt_event.event_id,
            payload={
                "attempt": attempt + 1,
                "code_chars": len(code),
                "raw_chars": len(generated_code),
            },
            tokens_out=max(1, len(generated_code) // 4),
        )
        traj.append(completion_event)
        last_root_completion_event = completion_event
        log.steps.append(
            {"step": "llm_generate", "attempt": attempt + 1, "code_len": len(code)}
        )

        # --- inject a fresh sub_llm bound to THIS root completion as parent ---
        # depth=0 parent → sub_llm runs at depth=1.
        try:
            session.inject_callable(
                "sub_llm",
                make_sub_llm(
                    trajectory=traj,
                    budget=budget,
                    parent_depth=0,
                    parent_event_id=completion_event.event_id,
                    timeout_s=30.0,
                ),
            )
        except BudgetExceeded as exc:
            log.error = "budget_exceeded"
            traj.error = f"budget_exceeded: {exc}"
            break
        except RecursionDepthExceeded as exc:
            log.error = "recursion_depth_exceeded"
            traj.error = f"recursion_depth_exceeded: {exc}"
            break

        # --- exec sandboxed code ---
        try:
            exec_result = session.exec(code)
        except BudgetExceeded as exc:
            log.error = "budget_exceeded"
            traj.error = f"budget_exceeded: {exc}"
            break
        except RecursionDepthExceeded as exc:
            log.error = "recursion_depth_exceeded"
            traj.error = f"recursion_depth_exceeded: {exc}"
            break
        except Exception as exc:  # noqa: BLE001
            log.error = "rlm_error"
            traj.error = f"rlm_error: {type(exc).__name__}: {str(exc)[:200]}"
            break

        # exec_result is SandboxResult (Phase 3); fall back gracefully.
        exec_error = getattr(exec_result, "error", None)
        exec_stdout = getattr(exec_result, "stdout", "")
        last_exec_error = exec_error

        traj.append(
            TrajectoryEvent.new(
                kind="repl_exec_error" if exec_error else "repl_exec",
                depth=0,
                parent_event_id=completion_event.event_id,
                payload={
                    "attempt": attempt + 1,
                    "error": exec_error,
                    "stdout_chars": len(exec_stdout),
                    "timed_out": getattr(exec_result, "timed_out", False),
                },
            )
        )
        log.steps.append(
            {
                "step": "exec",
                "attempt": attempt + 1,
                "output": exec_error or exec_stdout[:200],
            }
        )

        final_answer = session.get("FINAL_ANSWER")
        if final_answer is not None:
            break

        # Retry: feed back the exec error so the LLM can fix the code.
        messages.append({"role": "assistant", "content": generated_code})
        retry_hint = (
            f"FINAL_ANSWER was not set. Sandbox said: {exec_error or '(no error)'}.\n"
            "Rewrite the snippet, set FINAL_ANSWER as a list of dicts, "
            "and output Python only."
        )
        messages.append({"role": "user", "content": retry_hint})

    # Translate per-attempt structured errors into the legacy log.error.
    # (BudgetExceeded etc. set log.error inside the loop and `break`.)

    log.final_answer = final_answer

    if log.error is None and final_answer is None and last_exec_error:
        # All retries used and FINAL_ANSWER never set. Surface as rlm_error
        # rather than silently returning empty, per Phase 3 contract.
        log.error = "rlm_error"
        traj.error = f"rlm_error: FINAL_ANSWER never set; last exec_error={last_exec_error[:200]}"

    # 5: final_answer event
    traj.final_answer = final_answer
    traj.append(
        TrajectoryEvent.new(
            kind="final_answer",
            depth=0,
            parent_event_id=(
                last_root_completion_event.event_id
                if last_root_completion_event
                else root_prompt_event.event_id
            ),
            payload={
                "type": type(final_answer).__name__,
                "count": (len(final_answer) if isinstance(final_answer, list) else 0),
                "error": log.error,
            },
        )
    )

    log.elapsed_ms = (time.perf_counter() - t0) * 1000.0
    traj.elapsed_ms = log.elapsed_ms

    _maybe_persist(traj)

    logger.info(
        "RLM trajectory=%s query=%r events=%d final_answer_type=%s elapsed_ms=%.0f error=%s",
        trajectory_id,
        query,
        len(traj.events),
        type(final_answer).__name__,
        log.elapsed_ms,
        log.error,
    )
    return log


def _maybe_persist(traj: Trajectory) -> None:
    """Persist trajectory JSON when KOLAW_PERSIST_TRAJECTORY=1."""
    if os.getenv("KOLAW_PERSIST_TRAJECTORY", "0") != "1":
        return
    try:
        target = Path.home() / ".kolaw" / "trajectories"
        traj.persist(dir=target)
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        logger.warning("trajectory persist failed: %s", exc)


def _trajectory_to_response(
    log: TrajectoryLog, req: SearchRequest
) -> SearchResponse:
    """Convert TrajectoryLog to SearchResponse for the API."""
    if log.error:
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

    verdict: str = "ambiguous"
    confidence = 0.5
    if citations:
        verdict = "applies"
        confidence = 0.7
    elif log.final_answer is not None:
        verdict = "does_not_apply"
        confidence = 0.3

    return SearchResponse(
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        citations=citations,
        trajectory_id=log.trajectory_id,
        mode="deep",
    )


async def deep_search(req: SearchRequest) -> SearchResponse:
    """
    Phase 3 deep search via RLM loop with Trajectory + Budget + sub_llm.
    On failure: returns SearchResponse.error one of
    {budget_exceeded, recursion_depth_exceeded, local_llm_unavailable, rlm_error}.
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
