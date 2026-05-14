"""
RLM REPL — Phase 3.

Backed by the RestrictedPython sandbox in `services.rlm_engine.sandbox`.
Adds two Phase-3 features on top of the original Phase-1/2 surface:

  - inject_callable(name, fn): bind a host-side callable (e.g. `sub_llm`,
    `load_law`) into the REPL namespace. Bypasses RestrictedPython's
    name guards because the callable is provided by the host, not user code.
  - set_sandbox(name): swap the executor (currently only "restricted_python"
    is wired; "docker" is a Phase-4 hook).

Public surface preserved for the orchestrator: load(), get(), exec(), history.
`exec()` now returns a SandboxResult (was: str|None). Callers that only
log/format the result are unaffected because SandboxResult has a friendly repr.

Reference: arXiv 2512.24601v2 (Recursive Language Models).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from services.rlm_engine.sandbox import (
    ALLOWED_BUILTINS,
    SandboxResult,
    exec_restricted,
)

logger = logging.getLogger(__name__)


class RLMSession:
    """REPL session backed by a hardened sandbox."""

    def __init__(self, sandbox: str = "restricted_python"):
        self._namespace: dict[str, Any] = {}
        self._injected: dict[str, Callable] = {}
        self._sandbox = sandbox
        self._history: list[str] = []

    # --- Phase 1/2 surface (preserved) ---

    def load(self, key: str, value: Any) -> None:
        """Bind a value into the session namespace."""
        self._namespace[key] = value

    def get(self, name: str) -> Any:
        """Retrieve a named value from the session namespace."""
        return self._namespace.get(name)

    @property
    def history(self) -> list[str]:
        return list(self._history)

    # --- Phase 3 additions ---

    def inject_callable(self, name: str, fn: Callable) -> None:
        """
        Bind a host-side callable under `name`. Persisted across exec()s.
        """
        if not callable(fn):
            raise TypeError(f"inject_callable expects a callable, got {type(fn)!r}")
        self._injected[name] = fn

    def set_sandbox(self, name: str) -> None:
        """Select the sandbox backend. Only 'restricted_python' wired in Phase 3."""
        if name not in {"restricted_python"}:
            raise ValueError(
                f"unsupported sandbox {name!r}; expected 'restricted_python'"
            )
        self._sandbox = name

    # --- exec ---

    def exec(self, code: str, *, timeout_s: float = 10.0) -> SandboxResult:
        """
        Execute `code` in the sandbox.

        Returns a SandboxResult. Errors are surfaced via result.error rather
        than raised. Updates the session namespace with new bindings on
        success. Injected callables are merged into the namespace before
        execution (and are not exposed in the namespace_delta).
        """
        self._history.append(code)

        # Merge injected callables into the working namespace for this call.
        # We pass a copy so the sandbox can mutate it without confusing our
        # bookkeeping.
        working_ns: dict[str, Any] = dict(self._namespace)
        for name, fn in self._injected.items():
            working_ns[name] = fn

        result = exec_restricted(
            code,
            working_ns,
            timeout_s=timeout_s,
            allowed_builtins=ALLOWED_BUILTINS,
        )

        # Persist successful new/changed bindings into the session namespace,
        # but never persist the injected callables themselves.
        if result.error is None:
            for k, v in result.namespace_delta.items():
                if k in self._injected:
                    continue
                self._namespace[k] = v

        return result
