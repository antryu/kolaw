"""
RLM REPL sandbox — Phase 1 stub.

Implements RLMSession with load/exec/get interface.
Phase 1: uses plain exec() with a capped globals dict.
Phase 2: replace with RestrictedPython or Docker sandbox.

Reference: arXiv 2512.24601v2 (Recursive Language Models)
"""

from __future__ import annotations

import builtins
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Safe builtins allowed in exec sandbox (Phase 1: minimal set)
_SAFE_BUILTINS = {
    "len", "range", "enumerate", "zip", "map", "filter",
    "list", "dict", "set", "tuple", "str", "int", "float", "bool",
    "print", "repr", "sorted", "reversed", "min", "max", "sum",
    "isinstance", "type", "hasattr", "getattr",
}


def _make_safe_globals(namespace: dict[str, Any]) -> dict[str, Any]:
    safe = {k: getattr(builtins, k) for k in _SAFE_BUILTINS if hasattr(builtins, k)}
    safe["__builtins__"] = safe
    safe.update(namespace)
    return safe


class RLMSession:
    """
    Minimal REPL session for Phase 1.
    State is a flat dict; exec() runs code in that dict.
    """

    def __init__(self):
        self._namespace: dict[str, Any] = {}
        self._history: list[str] = []

    def load(self, key: str, value: Any) -> None:
        """Bind a value into the session namespace."""
        self._namespace[key] = value

    def exec(self, code: str) -> str | None:
        """
        Execute code string in the session namespace.

        Returns stdout-like string output or None.
        Captures exceptions rather than raising.
        Phase 1: no timeout; Phase 2 will add resource limits.
        """
        self._history.append(code)
        output_lines: list[str] = []

        def _print(*args, **kwargs):
            output_lines.append(" ".join(str(a) for a in args))

        safe_globals = _make_safe_globals(self._namespace)
        safe_globals["print"] = _print

        try:
            exec(code, safe_globals)  # noqa: S102 — Phase 2 will sandbox
            # Merge back any new names the code defined
            for k, v in safe_globals.items():
                if k not in ("__builtins__", "print"):
                    self._namespace[k] = v
        except Exception as exc:
            error_msg = f"ExecError: {type(exc).__name__}: {exc}"
            logger.warning("RLM exec error: %s", error_msg)
            return error_msg

        return "\n".join(output_lines) if output_lines else None

    def get(self, name: str) -> Any:
        """Retrieve a named value from the session namespace."""
        return self._namespace.get(name)

    @property
    def history(self) -> list[str]:
        return list(self._history)
