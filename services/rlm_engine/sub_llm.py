"""
RLM sub-LLM bridge — Phase 3.

`make_sub_llm(...)` returns a synchronous callable that the root LLM can
invoke from inside the REPL sandbox under the name `sub_llm`. The bridge:

  - enforces depth + sub-call budget,
  - records request/response/error events on the trajectory,
  - drives the async router.complete() from sync context,
  - returns a string on error rather than raising into the sandbox.

Reference: arXiv 2512.24601v2 (Recursive Language Models).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from services.rlm_engine.budget import (
    BudgetExceeded,
    RecursionDepthExceeded,
    TokenBudget,
)
from services.rlm_engine.trajectory import Trajectory, TrajectoryEvent

logger = logging.getLogger(__name__)


def _est_tokens(text: str) -> int:
    """Rough token estimate (chars/4) until we wire a real tokenizer."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _run_async(coro):
    """
    Drive an async coroutine from sync (RestrictedPython) context.

    If a loop is already running on this thread we'd raise — RestrictedPython
    code is not async, so we always create a fresh loop.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_sub_llm(
    trajectory: Trajectory,
    budget: TokenBudget,
    parent_depth: int,
    parent_event_id: str,
    timeout_s: float = 30.0,
) -> Callable[..., str]:
    """
    Build the sync callable to inject into the REPL as `sub_llm`.

    The returned callable accepts:
        prompt: str (positional or keyword)
        max_tokens: int = 512
        temperature: float = 0.2

    On any failure (budget exceeded, depth exceeded, router error) it
    returns a short string starting with "[sub_llm_error] ..." and records
    a `sub_llm_error` event on the trajectory. The root LLM is expected to
    treat that string as natural-language feedback.
    """

    def sub_llm(
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.2,
    ) -> str:
        depth = parent_depth + 1

        # 1. Depth check
        try:
            budget.assert_depth(depth)
        except RecursionDepthExceeded as exc:
            msg = f"[sub_llm_error] recursion depth exceeded: {exc}"
            trajectory.append(
                TrajectoryEvent.new(
                    kind="sub_llm_error",
                    depth=depth,
                    payload={"reason": "recursion_depth", "detail": str(exc)},
                    parent_event_id=parent_event_id,
                )
            )
            return msg

        # 2. Sub-call cap
        try:
            budget.increment_sub_call()
        except BudgetExceeded as exc:
            msg = f"[sub_llm_error] sub-call budget exceeded: {exc}"
            trajectory.append(
                TrajectoryEvent.new(
                    kind="sub_llm_error",
                    depth=depth,
                    payload={"reason": "max_sub_calls", "detail": str(exc)},
                    parent_event_id=parent_event_id,
                )
            )
            return msg

        # 3. Reservation (best-effort estimate)
        prompt_text = str(prompt) if prompt is not None else ""
        est_in = _est_tokens(prompt_text)
        est_out = min(int(max_tokens), budget.per_call_out_cap)
        if not budget.reserve(est_in, est_out, depth):
            msg = (
                f"[sub_llm_error] reservation refused "
                f"(est_in={est_in}, est_out={est_out}, depth={depth})"
            )
            trajectory.append(
                TrajectoryEvent.new(
                    kind="sub_llm_error",
                    depth=depth,
                    payload={
                        "reason": "reservation_refused",
                        "est_in": est_in,
                        "est_out": est_out,
                    },
                    parent_event_id=parent_event_id,
                )
            )
            return msg

        # 4. Record request event
        request_event = TrajectoryEvent.new(
            kind="sub_llm_request",
            depth=depth,
            payload={
                "prompt": prompt_text,
                "max_tokens": est_out,
                "temperature": temperature,
            },
            parent_event_id=parent_event_id,
            tokens_in=est_in,
        )
        trajectory.append(request_event)

        # 5. Drive router.complete (async) from sync context.
        from services.llm import router  # imported here so tests can monkeypatch

        messages = [{"role": "user", "content": prompt_text}]
        started = time.perf_counter()
        try:
            response = _run_async(
                asyncio.wait_for(
                    router.complete(
                        messages,
                        max_tokens=est_out,
                        temperature=float(temperature),
                    ),
                    timeout=timeout_s,
                )
            )
        except asyncio.TimeoutError:
            msg = f"[sub_llm_error] timed out after {timeout_s}s"
            trajectory.append(
                TrajectoryEvent.new(
                    kind="sub_llm_error",
                    depth=depth,
                    payload={"reason": "timeout", "timeout_s": timeout_s},
                    parent_event_id=request_event.event_id,
                )
            )
            # Best-effort consume of input estimate so the budget reflects
            # work already attempted.
            try:
                budget.consume(est_in, 0)
            except BudgetExceeded:
                pass
            return msg
        except Exception as exc:
            msg = f"[sub_llm_error] {type(exc).__name__}: {exc}"
            trajectory.append(
                TrajectoryEvent.new(
                    kind="sub_llm_error",
                    depth=depth,
                    payload={
                        "reason": "router_error",
                        "exc_type": type(exc).__name__,
                        "detail": str(exc),
                    },
                    parent_event_id=request_event.event_id,
                )
            )
            try:
                budget.consume(est_in, 0)
            except BudgetExceeded:
                pass
            return msg

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        actual_in = est_in
        actual_out = _est_tokens(response if isinstance(response, str) else "")

        # 6. Consume budget with actuals
        try:
            budget.consume(actual_in, actual_out)
        except BudgetExceeded as exc:
            # Surface but don't mask the response — callers may still want
            # the partial answer; record the breach.
            trajectory.append(
                TrajectoryEvent.new(
                    kind="sub_llm_error",
                    depth=depth,
                    payload={
                        "reason": "budget_overrun",
                        "detail": str(exc),
                        "tokens_in": actual_in,
                        "tokens_out": actual_out,
                    },
                    parent_event_id=request_event.event_id,
                )
            )

        # 7. Record response event
        trajectory.append(
            TrajectoryEvent.new(
                kind="sub_llm_response",
                depth=depth,
                payload={
                    "response": response,
                    "elapsed_ms": elapsed_ms,
                },
                parent_event_id=request_event.event_id,
                tokens_in=actual_in,
                tokens_out=actual_out,
            )
        )

        return response if isinstance(response, str) else str(response)

    return sub_llm
