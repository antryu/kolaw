"""
test_rlm_sandbox.py — Phase 4 hardening tests.

Verifies that the RestrictedPython-backed RLMSession:
  * runs ordinary list/dict code that the LLM emits in production
  * blocks dangerous primitives (`import`, `__class__` walks, file I/O)
  * surfaces compile / runtime / timeout errors as structured strings
    so the orchestrator can feed them back into the multi-turn retry.
"""

from __future__ import annotations

import pytest

from services.rlm_engine.repl import RLMSession, _HAS_RESTRICTED


pytestmark = pytest.mark.skipif(
    not _HAS_RESTRICTED,
    reason="RestrictedPython not installed; sandbox tests are no-ops",
)


class TestSandboxHappyPath:
    def test_set_final_answer_simple(self):
        s = RLMSession()
        s.exec("FINAL_ANSWER = [{'law_id': 'x', 'law_name': 'y'}]")
        assert s.get("FINAL_ANSWER") == [{"law_id": "x", "law_name": "y"}]

    def test_dict_iteration_works(self):
        s = RLMSession()
        s.load("law_texts", {"수소법": "이 법은 수소경제를 위한 것이다", "도시가스법": "도시가스 안전관리"})
        s.exec(
            "hits = []\n"
            "for name, text in law_texts.items():\n"
            "    if '수소' in text:\n"
            "        hits.append({'law_id': 'mock', 'law_name': name, 'article': '제1조', 'excerpt': text[:30]})\n"
            "FINAL_ANSWER = hits"
        )
        ans = s.get("FINAL_ANSWER")
        assert isinstance(ans, list) and len(ans) == 1
        assert ans[0]["law_name"] == "수소법"

    def test_list_comprehension_and_slice(self):
        s = RLMSession()
        s.load("items", [{"id": i, "v": i * 2} for i in range(5)])
        s.exec("FINAL_ANSWER = [it for it in items if it['v'] >= 4][:2]")
        assert [it["id"] for it in s.get("FINAL_ANSWER")] == [2, 3]

    def test_namespace_persists_across_exec(self):
        s = RLMSession()
        s.exec("counter = 0\ncounter = counter + 5")
        s.exec("FINAL_ANSWER = counter * 2")
        assert s.get("FINAL_ANSWER") == 10


class TestSandboxBlocksDangerousOps:
    def test_blocks_import(self):
        s = RLMSession()
        out = s.exec("import os\nFINAL_ANSWER = []")
        assert out is not None
        assert "CompileError" in out or "ExecError" in out

    def test_blocks_dunder_access(self):
        s = RLMSession()
        # Attempt the classic __class__.__bases__[0].__subclasses__() chain.
        out = s.exec("FINAL_ANSWER = [].__class__.__bases__[0].__subclasses__()")
        assert out is not None
        # RestrictedPython rejects names starting with "_" at compile time.
        assert "CompileError" in out or "ExecError" in out
        # And FINAL_ANSWER must NOT have been set to the subclass list.
        assert s.get("FINAL_ANSWER") is None or s.get("FINAL_ANSWER") == []

    def test_blocks_open(self):
        s = RLMSession()
        out = s.exec("open('/etc/passwd').read()")
        assert out is not None
        # `open` is not in safe_builtins, so this is an ExecError (NameError).
        assert "ExecError" in out or "CompileError" in out


class TestSandboxErrorShape:
    def test_syntax_error_structured(self):
        s = RLMSession()
        out = s.exec("FINAL_ANSWER = [")
        assert out is not None
        assert out.startswith(("CompileError:", "SyntaxError:"))

    def test_runtime_error_structured(self):
        s = RLMSession()
        s.load("d", {"a": 1})
        out = s.exec("FINAL_ANSWER = d['missing']")
        assert out is not None
        assert out.startswith("ExecError:")
        assert "KeyError" in out


class TestOrchestratorRetryHints:
    """Verify orchestrator.run feeds the new branched retry messages."""

    @pytest.mark.asyncio
    async def test_retry_message_branches_on_runtime_error(self):
        """When code raises at runtime, the retry hint should mention runtime fix-ups."""
        from unittest.mock import AsyncMock, patch

        from services.rlm_engine.orchestrator import run

        captured_messages: list[list[dict]] = []

        async def mock_complete(messages, **kwargs):
            captured_messages.append([dict(m) for m in messages])
            if len(captured_messages) == 1:
                # First attempt: forbidden import → ExecError (ImportError).
                return "import os\nFINAL_ANSWER = []"
            return "FINAL_ANSWER = [{'law_id':'a','law_name':'b','article':'c','excerpt':'d'}]"

        with patch("services.llm.router.complete", AsyncMock(side_effect=mock_complete)):
            with patch("services.fast_search.search._get_collection") as mock_col:
                mock_col.return_value.query.return_value = {
                    "documents": [[]], "metadatas": [[]], "distances": [[]],
                }
                log = await run(query="retry hint test")

        assert len(captured_messages) >= 2, "Should retry after runtime error"
        retry_user_msg = captured_messages[1][-1]
        assert retry_user_msg["role"] == "user"
        # `import os` survives compile but fails at runtime → ExecError branch fires.
        assert "raised at runtime" in retry_user_msg["content"]
        assert "ExecError" in retry_user_msg["content"]
        assert log.final_answer is not None

    @pytest.mark.asyncio
    async def test_retry_message_branches_on_compile_error(self):
        """When code triggers RestrictedPython's compile-time policy, the hint must say so."""
        from unittest.mock import AsyncMock, patch

        from services.rlm_engine.orchestrator import run

        captured_messages: list[list[dict]] = []

        async def mock_complete(messages, **kwargs):
            captured_messages.append([dict(m) for m in messages])
            if len(captured_messages) == 1:
                # `_private` name access is rejected at compile time by RestrictedPython.
                return "_x = 1\nFINAL_ANSWER = []"
            return "FINAL_ANSWER = [{'law_id':'a','law_name':'b','article':'c','excerpt':'d'}]"

        with patch("services.llm.router.complete", AsyncMock(side_effect=mock_complete)):
            with patch("services.fast_search.search._get_collection") as mock_col:
                mock_col.return_value.query.return_value = {
                    "documents": [[]], "metadatas": [[]], "distances": [[]],
                }
                log = await run(query="compile branch test")

        assert len(captured_messages) >= 2
        retry_user_msg = captured_messages[1][-1]
        assert (
            "did not compile" in retry_user_msg["content"]
            or "sandbox" in retry_user_msg["content"]
        )
        assert log.final_answer is not None
