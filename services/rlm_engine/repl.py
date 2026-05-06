"""
RLM REPL sandbox — Phase 4 (hardened).

Implements RLMSession with load/exec/get interface backed by RestrictedPython.

What changed from Phase 1:
- exec() now compiles via RestrictedPython.compile_restricted_exec which:
    * blocks `import`, `__import__`, dunder access (__class__.__bases__ chain)
    * blocks attribute access to private names (`_x`, `__x__`)
    * disallows file I/O / subprocess via the restricted builtin set
- A per-exec timeout (signal-based, Unix only) prevents infinite loops in
  the LLM-emitted code from hanging the request.
- On compile failure / runtime error / timeout the session returns a
  structured error string the orchestrator can feed back to the LLM for a
  multi-turn retry.

If RestrictedPython is unavailable (no install on this host) we fall back
to the Phase-1 behaviour: plain exec with a capped builtin set. That keeps
unit tests runnable on a bare checkout; production hosts must have
RestrictedPython installed.

Reference: arXiv 2512.24601v2 (Recursive Language Models) §4.
"""

from __future__ import annotations

import builtins
import logging
import os
import signal
from typing import Any

logger = logging.getLogger(__name__)

# Default per-exec wall-clock timeout (seconds). Override via RLM_EXEC_TIMEOUT.
_DEFAULT_EXEC_TIMEOUT = float(os.getenv("RLM_EXEC_TIMEOUT", "5.0"))

# Phase-1 fallback builtins (used only when RestrictedPython is missing).
_SAFE_BUILTINS = {
    "len", "range", "enumerate", "zip", "map", "filter",
    "list", "dict", "set", "tuple", "str", "int", "float", "bool",
    "print", "repr", "sorted", "reversed", "min", "max", "sum",
    "isinstance", "type", "hasattr", "getattr",
}

try:
    from RestrictedPython import compile_restricted_exec, safe_globals
    from RestrictedPython.Eval import default_guarded_getiter
    from RestrictedPython.Guards import (
        guarded_iter_unpack_sequence,
        guarded_unpack_sequence,
        safe_builtins,
    )

    _HAS_RESTRICTED = True
except Exception as _exc:  # pragma: no cover — missing optional dep
    logger.warning("RestrictedPython unavailable, falling back to plain exec: %s", _exc)
    _HAS_RESTRICTED = False


class _ExecTimeout(Exception):
    """Raised when an exec() exceeds RLM_EXEC_TIMEOUT seconds."""


def _install_alarm(seconds: float) -> bool:
    """Best-effort SIGALRM timeout. Returns True if armed.

    SIGALRM is Unix-only and only fires from the main thread; under uvicorn
    workers it still fires because the worker handler runs on the main
    thread of its process. If we are not on the main thread we silently
    skip the timer — the LLM still has token-level guardrails upstream.
    """
    if not hasattr(signal, "SIGALRM"):
        return False
    try:
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(_ExecTimeout()))
        signal.setitimer(signal.ITIMER_REAL, seconds)
        return True
    except (ValueError, OSError):
        # ValueError: not main thread. OSError: signal already in use.
        return False


def _cancel_alarm() -> None:
    if hasattr(signal, "SIGALRM"):
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
        except (ValueError, OSError):
            pass


def _make_safe_globals_fallback(namespace: dict[str, Any]) -> dict[str, Any]:
    safe = {k: getattr(builtins, k) for k in _SAFE_BUILTINS if hasattr(builtins, k)}
    safe["__builtins__"] = safe
    safe.update(namespace)
    return safe


def _make_restricted_globals(
    namespace: dict[str, Any], printer_fn,
) -> dict[str, Any]:
    """Build a RestrictedPython exec globals dict for a single run."""
    g: dict[str, Any] = dict(safe_globals)
    g["__builtins__"] = dict(safe_builtins)

    # Whitelist a handful of read-only builtins the LLM legitimately uses
    # to walk law_texts / live_results dicts. RestrictedPython's
    # safe_builtins omits these by default.
    for name in (
        "list", "dict", "set", "tuple",
        "len", "range", "enumerate", "zip", "map", "filter",
        "min", "max", "sum", "sorted", "reversed",
        "str", "int", "float", "bool",
        "isinstance", "type", "repr",
        "any", "all",
    ):
        if hasattr(builtins, name):
            g["__builtins__"][name] = getattr(builtins, name)

    # RestrictedPython rewrites attribute reads to call _getattr_, etc.
    # We map them to their permissive (but still safe) defaults so dict /
    # list access works without false positives.
    g["_getattr_"] = getattr
    g["_getitem_"] = lambda obj, idx: obj[idx]
    g["_getiter_"] = default_guarded_getiter
    g["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence
    g["_unpack_sequence_"] = guarded_unpack_sequence
    g["_write_"] = lambda x: x  # we don't whitelist any mutable writes; bind via locals
    g["_print_"] = type(
        "_PrintCollector", (), {
            "__init__": lambda self, _ignored=None: None,
            "write": lambda self, text: printer_fn(text.rstrip("\n")) if text.strip() else None,
            "_call_print": lambda self, *args, **kwargs: printer_fn(
                " ".join(str(a) for a in args)
            ),
        },
    )
    g.update(namespace)
    return g


class RLMSession:
    """REPL session for the RLM loop. State persists across exec() calls.

    The session keeps a flat namespace dict. Each exec() call:
    1. compiles the LLM-emitted code under RestrictedPython
    2. runs it with a SIGALRM-based wall-clock timeout
    3. captures any print() output and merges new bindings back into
       the namespace so subsequent exec() calls can use them.
    """

    def __init__(self, exec_timeout: float | None = None):
        self._namespace: dict[str, Any] = {}
        self._history: list[str] = []
        self._timeout = float(exec_timeout) if exec_timeout else _DEFAULT_EXEC_TIMEOUT

    def load(self, key: str, value: Any) -> None:
        self._namespace[key] = value

    def exec(self, code: str) -> str | None:
        self._history.append(code)
        output_lines: list[str] = []

        def _print(*args, **kwargs):
            output_lines.append(" ".join(str(a) for a in args))

        if _HAS_RESTRICTED:
            try:
                compiled = compile_restricted_exec(code, filename="<rlm-repl>")
            except SyntaxError as exc:
                return f"CompileError: SyntaxError: {exc}"

            if compiled.errors:
                return "CompileError: " + " | ".join(compiled.errors)
            for w in compiled.warnings or []:
                logger.debug("RLM compile warning: %s", w)

            exec_globals = _make_restricted_globals(self._namespace, _print)
            target = compiled.code
        else:
            exec_globals = _make_safe_globals_fallback(self._namespace)
            exec_globals["print"] = _print
            try:
                target = compile(code, "<rlm-repl>", "exec")
            except SyntaxError as exc:
                return f"CompileError: SyntaxError: {exc}"

        armed = _install_alarm(self._timeout)
        try:
            exec(target, exec_globals)  # noqa: S102 — sandboxed via RestrictedPython
        except _ExecTimeout:
            return f"TimeoutError: exec exceeded {self._timeout:.1f}s"
        except Exception as exc:
            error_msg = f"ExecError: {type(exc).__name__}: {exc}"
            logger.warning("RLM exec error: %s", error_msg)
            return error_msg
        finally:
            if armed:
                _cancel_alarm()

        # Merge user-defined names back into the persistent namespace.
        skip = {
            "__builtins__", "_getattr_", "_getitem_", "_getiter_",
            "_iter_unpack_sequence_", "_unpack_sequence_",
            "_write_", "_print_", "print",
        }
        for k, v in exec_globals.items():
            if k.startswith("__") or k in skip:
                continue
            self._namespace[k] = v

        return "\n".join(output_lines) if output_lines else None

    def get(self, name: str) -> Any:
        return self._namespace.get(name)

    @property
    def history(self) -> list[str]:
        return list(self._history)
