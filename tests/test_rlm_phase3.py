"""
test_rlm_phase3.py — RLM Phase 3 foundation layer tests.

Covers:
  - Trajectory serialization round-trip
  - TokenBudget caps (depth + max_sub_calls)
  - Sandbox compile-time/runtime blocks for forbidden constructs
  - Sandbox happy path + namespace delta
  - sub_llm callable records request/response on the trajectory
  - sub_llm error path returns a string and records sub_llm_error
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from services.rlm_engine.budget import (
    BudgetExceeded,
    RecursionDepthExceeded,
    TokenBudget,
)
from services.rlm_engine.sandbox import (
    SandboxCompileError,
    SandboxResult,
    compile_restricted_code,
    exec_restricted,
)
from services.rlm_engine.sub_llm import make_sub_llm
from services.rlm_engine.trajectory import Trajectory, TrajectoryEvent


# --------------------------------------------------------------------- #
# 1. Trajectory round-trip                                              #
# --------------------------------------------------------------------- #

class TestTrajectoryRoundTrip:
    def test_to_dict_from_dict_equivalence(self):
        traj = Trajectory.new(query="수소충전소 허가 요건")
        traj.append(
            TrajectoryEvent.new(kind="root_prompt", depth=0, payload={"q": "x"})
        )
        traj.append(
            TrajectoryEvent.new(
                kind="repl_exec",
                depth=0,
                payload={"code": "FINAL_ANSWER = []"},
            )
        )
        traj.append(
            TrajectoryEvent.new(
                kind="sub_llm_request",
                depth=1,
                payload={"prompt": "summarize"},
                tokens_in=12,
            )
        )
        traj.append(
            TrajectoryEvent.new(
                kind="sub_llm_response",
                depth=1,
                payload={"response": "ok"},
                tokens_out=3,
            )
        )
        traj.append(
            TrajectoryEvent.new(
                kind="final_answer", depth=0, payload={"items": []}
            )
        )
        traj.final_answer = []
        traj.elapsed_ms = 42.0

        as_dict = traj.to_dict()
        # Must be JSON-serializable
        encoded = json.dumps(as_dict, ensure_ascii=False, default=str)
        decoded = json.loads(encoded)
        rt = Trajectory.from_dict(decoded)

        assert rt.trajectory_id == traj.trajectory_id
        assert rt.query == traj.query
        assert rt.elapsed_ms == traj.elapsed_ms
        assert rt.final_answer == traj.final_answer
        assert len(rt.events) == 5
        for orig, restored in zip(traj.events, rt.events):
            assert orig.event_id == restored.event_id
            assert orig.kind == restored.kind
            assert orig.depth == restored.depth
            assert orig.payload == restored.payload
            assert orig.tokens_in == restored.tokens_in
            assert orig.tokens_out == restored.tokens_out

    def test_persist_and_load(self, tmp_path: Path):
        traj = Trajectory.new(query="test")
        traj.append(
            TrajectoryEvent.new(kind="root_prompt", depth=0, payload={"q": "x"})
        )
        path = traj.persist(dir=tmp_path)
        assert path.exists()
        loaded = Trajectory.load(path)
        assert loaded.trajectory_id == traj.trajectory_id
        assert loaded.events[0].kind == "root_prompt"


# --------------------------------------------------------------------- #
# 2. Budget caps                                                        #
# --------------------------------------------------------------------- #

class TestTokenBudget:
    def test_assert_depth_raises(self):
        budget = TokenBudget(max_depth=2)
        budget.assert_depth(0)
        budget.assert_depth(2)
        with pytest.raises(RecursionDepthExceeded):
            budget.assert_depth(3)

    def test_increment_sub_call_raises_at_cap(self):
        budget = TokenBudget(max_sub_calls=3)
        budget.increment_sub_call()
        budget.increment_sub_call()
        budget.increment_sub_call()
        with pytest.raises(BudgetExceeded):
            budget.increment_sub_call()

    def test_consume_raises_when_total_in_exceeded(self):
        budget = TokenBudget(total_in_cap=100, total_out_cap=100)
        budget.consume(50, 0)
        budget.consume(50, 0)
        with pytest.raises(BudgetExceeded):
            budget.consume(1, 0)

    def test_reserve_returns_false_when_depth_too_deep(self):
        budget = TokenBudget(max_depth=1)
        assert budget.reserve(10, 10, depth=1) is True
        assert budget.reserve(10, 10, depth=2) is False

    def test_remaining_decrements(self):
        budget = TokenBudget(total_in_cap=1000, total_out_cap=1000, max_sub_calls=5)
        budget.consume(100, 50)
        budget.increment_sub_call()
        rem = budget.remaining()
        assert rem["in"] == 900
        assert rem["out"] == 950
        assert rem["sub_calls"] == 4


# --------------------------------------------------------------------- #
# 3. Sandbox blocks                                                     #
# --------------------------------------------------------------------- #

class TestSandboxBlocks:
    @pytest.mark.parametrize(
        "code",
        [
            "__import__('os')",
            "eval('1+1')",
            "exec('1+1')",
            "().__class__.__base__.__subclasses__()",
            "compile('1','','eval')",
        ],
    )
    def test_compile_or_runtime_blocks(self, code: str):
        result = exec_restricted(code, {})
        assert result.error is not None, f"expected block for {code!r}"
        # Must NOT have leaked anything dangerous into the namespace
        assert result.namespace_delta == {}

    def test_open_blocked_at_runtime(self):
        # `open` passes the AST check (not dunder) but is missing from
        # ALLOWED_BUILTINS, so it errors at runtime.
        result = exec_restricted("open('/etc/passwd')", {})
        assert result.error is not None
        assert "NameError" in result.error or "open" in result.error

    def test_compile_restricted_code_raises_for_forbidden(self):
        with pytest.raises(SandboxCompileError):
            compile_restricted_code("__import__('os')")


# --------------------------------------------------------------------- #
# 4. Sandbox happy path                                                 #
# --------------------------------------------------------------------- #

class TestSandboxHappyPath:
    def test_list_comprehension(self):
        ns: dict = {}
        result = exec_restricted("result = [x*2 for x in range(5)]", ns)
        assert result.error is None
        assert result.namespace_delta.get("result") == [0, 2, 4, 6, 8]
        # caller namespace mutated in-place
        assert ns["result"] == [0, 2, 4, 6, 8]

    def test_underscore_keys_excluded_from_delta(self):
        # RestrictedPython blocks user-source `_name = ...` at compile time,
        # so we seed an underscore key via the host namespace and verify the
        # filter still hides it from namespace_delta.
        ns: dict = {"_seeded": "hidden"}
        result = exec_restricted("public = 2", ns)
        assert result.error is None
        assert result.namespace_delta.get("public") == 2
        assert "_seeded" not in result.namespace_delta
        # Sandbox hook keys must not leak either.
        for hook in ("_getattr_", "_getitem_", "_getiter_", "_print_", "_print"):
            assert hook not in result.namespace_delta

    def test_print_capture(self):
        ns: dict = {}
        result = exec_restricted('print("hello", "world")', ns)
        assert result.error is None
        assert "hello" in result.stdout
        assert "world" in result.stdout

    def test_namespace_input_preserved(self):
        ns = {"law_texts": {"수소법": "내용..."}}
        result = exec_restricted(
            "names = list(law_texts.keys())", ns
        )
        assert result.error is None
        assert result.namespace_delta.get("names") == ["수소법"]


# --------------------------------------------------------------------- #
# 5. sub_llm callable                                                   #
# --------------------------------------------------------------------- #

class TestSubLLM:
    def test_records_request_and_response(self):
        traj = Trajectory.new(query="parent query")
        budget = TokenBudget(max_depth=3, max_sub_calls=4)
        parent_event = TrajectoryEvent.new(
            kind="repl_exec", depth=0, payload={}
        )
        traj.append(parent_event)

        async def fake_complete(messages, **kw):
            return "sub-response-text"

        with patch("services.llm.router.complete", side_effect=fake_complete):
            sub_llm = make_sub_llm(
                trajectory=traj,
                budget=budget,
                parent_depth=0,
                parent_event_id=parent_event.event_id,
            )
            answer = sub_llm("Summarize 수소법 §44.")

        assert answer == "sub-response-text"

        kinds = [e.kind for e in traj.events]
        assert kinds.count("sub_llm_request") == 1
        assert kinds.count("sub_llm_response") == 1
        assert "sub_llm_error" not in kinds

        req_evt = next(e for e in traj.events if e.kind == "sub_llm_request")
        resp_evt = next(e for e in traj.events if e.kind == "sub_llm_response")
        assert req_evt.depth == 1
        assert resp_evt.depth == 1
        assert resp_evt.parent_event_id == req_evt.event_id

    def test_router_error_returns_string_and_records_event(self):
        traj = Trajectory.new(query="parent query")
        budget = TokenBudget(max_depth=3, max_sub_calls=4)
        parent_event = TrajectoryEvent.new(
            kind="repl_exec", depth=0, payload={}
        )
        traj.append(parent_event)

        async def boom(messages, **kw):
            raise RuntimeError("Local LLM failed and ALLOW_ANTHROPIC is not set.")

        with patch("services.llm.router.complete", side_effect=boom):
            sub_llm = make_sub_llm(
                trajectory=traj,
                budget=budget,
                parent_depth=0,
                parent_event_id=parent_event.event_id,
            )
            answer = sub_llm("anything")

        assert isinstance(answer, str)
        assert answer.startswith("[sub_llm_error]")

        kinds = [e.kind for e in traj.events]
        assert "sub_llm_request" in kinds
        assert "sub_llm_error" in kinds
        # response event must NOT have been recorded
        assert "sub_llm_response" not in kinds

    def test_depth_cap_blocks_before_router_call(self):
        traj = Trajectory.new(query="parent")
        budget = TokenBudget(max_depth=1)
        parent_event = TrajectoryEvent.new(kind="repl_exec", depth=1, payload={})
        traj.append(parent_event)

        # parent_depth=1 -> child depth=2 -> exceeds max_depth=1
        # router.complete must never be called
        called = {"n": 0}

        async def should_not_call(messages, **kw):  # pragma: no cover
            called["n"] += 1
            return "x"

        with patch("services.llm.router.complete", side_effect=should_not_call):
            sub_llm = make_sub_llm(
                trajectory=traj,
                budget=budget,
                parent_depth=1,
                parent_event_id=parent_event.event_id,
            )
            answer = sub_llm("anything")

        assert called["n"] == 0
        assert answer.startswith("[sub_llm_error]")
        kinds = [e.kind for e in traj.events]
        assert kinds.count("sub_llm_error") == 1
        assert "sub_llm_request" not in kinds
