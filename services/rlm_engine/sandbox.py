"""
RLM Sandbox — Phase 3 hardened executor (RestrictedPython + signal/resource).

Replaces the Phase-1 plain-exec sandbox with:
  - RestrictedPython compile-time AST checks (blocks _attr, eval, exec,
    __import__, dunder access, etc.).
  - Allowed-builtins whitelist (no open, no __import__, no globals, ...).
  - signal.SIGALRM wall-clock timeout (Unix only).
  - resource.RLIMIT_AS soft memory cap (best-effort).

Reference: arXiv 2512.24601v2 (Recursive Language Models).
"""

from __future__ import annotations

import builtins as _real_builtins
import logging
import signal
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from RestrictedPython import PrintCollector, compile_restricted
from RestrictedPython.Eval import (
    default_guarded_getitem,
    default_guarded_getiter,
)
from RestrictedPython.Guards import (
    full_write_guard,
    guarded_iter_unpack_sequence,
    safe_builtins as _rp_safe_builtins,
)

logger = logging.getLogger(__name__)


class SandboxCompileError(Exception):
    """Raised when user code fails RestrictedPython AST compilation."""


class SandboxRuntimeError(Exception):
    """Raised when sandboxed code raises during execution."""


@dataclass
class SandboxResult:
    stdout: str = ""
    namespace_delta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    timed_out: bool = False


# --- Allowed builtins ---------------------------------------------------
# Start from RestrictedPython's safe_builtins (no open/exec/eval/import) and
# add a few iterator helpers we need for typical legal-research code.
ALLOWED_BUILTINS: dict[str, Any] = dict(_rp_safe_builtins)
for _extra in (
    "list",
    "dict",
    "set",
    "frozenset",
    "tuple",
    "min",
    "max",
    "sum",
    "any",
    "all",
    "enumerate",
    "filter",
    "map",
    "reversed",
    "iter",
    "next",
    "print",
    "type",
    "hasattr",
    "getattr",
    "object",
    "Ellipsis",
    "NotImplemented",
):
    if hasattr(_real_builtins, _extra):
        ALLOWED_BUILTINS.setdefault(_extra, getattr(_real_builtins, _extra))


def _safe_getattr(obj: Any, name: str, *default: Any) -> Any:
    """getattr guard that refuses dunder lookups."""
    if isinstance(name, str) and name.startswith("_"):
        raise AttributeError(
            f"access to attribute {name!r} is not allowed in sandbox"
        )
    return getattr(obj, name, *default)


def _safe_getitem(obj: Any, key: Any) -> Any:
    """Delegate to RestrictedPython's default item guard."""
    return default_guarded_getitem(obj, key)


def _safe_getiter(obj: Any):
    return default_guarded_getiter(obj)


def _build_globals(
    namespace: dict[str, Any],
    allowed_builtins: dict[str, Any],
) -> dict[str, Any]:
    """Build the globals dict passed to exec()."""
    builtins_map = dict(allowed_builtins)
    # RestrictedPython requires _getattr_/_getitem_/_getiter_ hooks on the
    # globals (NOT inside __builtins__).
    g: dict[str, Any] = {
        "__builtins__": builtins_map,
        "_getattr_": _safe_getattr,
        "_getitem_": _safe_getitem,
        "_getiter_": _safe_getiter,
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        "_write_": full_write_guard,
        "_inplacevar_": _inplacevar,
        "_print_": PrintCollector,
    }
    g.update(namespace)
    return g


def _inplacevar(op: str, x: Any, y: Any) -> Any:
    """Minimal in-place op support so `+=`/`-=` work in sandboxed code."""
    if op == "+=":
        return x + y
    if op == "-=":
        return x - y
    if op == "*=":
        return x * y
    if op == "/=":
        return x / y
    if op == "//=":
        return x // y
    if op == "%=":
        return x % y
    if op == "**=":
        return x ** y
    raise SandboxRuntimeError(f"unsupported in-place op: {op}")


def compile_restricted_code(code: str) -> Any:
    """
    Compile a snippet under RestrictedPython rules.

    Returns a CodeType. Raises SandboxCompileError if the source contains
    forbidden constructs (eval, exec, __import__, dunder access, ...).
    """
    try:
        compiled = compile_restricted(code, "<rlm-sandbox>", "exec")
    except SyntaxError as exc:
        raise SandboxCompileError(str(exc)) from exc
    if compiled is None:
        raise SandboxCompileError("RestrictedPython refused to compile snippet")
    return compiled


# --- Resource & timeout guards -----------------------------------------

class _Timeout(Exception):
    """Internal — not surfaced to callers; converted to SandboxResult."""


@contextmanager
def _alarm_timeout(seconds: float):
    """SIGALRM-based wall-clock timeout. Unix only; main thread only.

    Python's signal handlers can only be installed from the main thread of
    the main interpreter. When sandbox.exec runs inside a worker thread
    (FastAPI TestClient, asyncio.to_thread, ThreadPoolExecutor, ...) we
    silently skip the wall-clock guard. The budget + memory caps still apply.
    """
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handler(signum, frame):  # pragma: no cover - signal context
        raise _Timeout(f"sandbox exec exceeded {seconds}s")

    # signal.setitimer takes float seconds; SIGALRM is integer-only.
    try:
        prev_handler = signal.signal(signal.SIGALRM, _handler)
    except ValueError:
        # Not in main thread — timeout cannot be installed. Skip silently.
        yield
        return
    try:
        signal.setitimer(signal.ITIMER_REAL, float(seconds))
    except (ValueError, OSError):
        signal.signal(signal.SIGALRM, prev_handler)
        yield
        return
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, prev_handler)


@contextmanager
def _memory_limit(memory_mb: int):
    """RLIMIT_AS soft cap. Best-effort; silently skipped if unsupported."""
    if memory_mb <= 0:
        yield
        return
    try:
        import resource  # POSIX-only
    except ImportError:  # pragma: no cover - Windows
        yield
        return

    bytes_cap = int(memory_mb) * 1024 * 1024
    try:
        prev = resource.getrlimit(resource.RLIMIT_AS)
    except (ValueError, OSError):
        yield
        return
    try:
        # Don't lower below current usage — best-effort only.
        try:
            resource.setrlimit(resource.RLIMIT_AS, (bytes_cap, prev[1]))
        except (ValueError, OSError) as exc:
            logger.debug("RLIMIT_AS not enforced: %s", exc)
        yield
    finally:
        try:
            resource.setrlimit(resource.RLIMIT_AS, prev)
        except (ValueError, OSError):
            pass


# --- Public API ---------------------------------------------------------

def exec_restricted(
    code: str,
    namespace: dict[str, Any],
    timeout_s: float = 10.0,
    memory_mb: int = 256,
    allowed_builtins: dict[str, Any] | None = None,
) -> SandboxResult:
    """
    Execute `code` inside the RestrictedPython sandbox.

    Args:
        code: Python source string.
        namespace: pre-bound names available to the snippet (also receives
            new bindings on success).
        timeout_s: wall-clock cap (SIGALRM, Unix only).
        memory_mb: best-effort RLIMIT_AS soft cap.
        allowed_builtins: override the default ALLOWED_BUILTINS map.

    Returns:
        SandboxResult with captured stdout, namespace delta, and either
        `error` (string) or `timed_out=True` on failure. Never raises;
        all execution failures are surfaced via the result fields.
    """
    builtins_map = allowed_builtins if allowed_builtins is not None else ALLOWED_BUILTINS

    try:
        compiled = compile_restricted_code(code)
    except SandboxCompileError as exc:
        return SandboxResult(error=f"SandboxCompileError: {exc}")

    pre_keys = set(namespace.keys())
    g = _build_globals(namespace, builtins_map)

    timed_out = False
    error: str | None = None
    try:
        with _alarm_timeout(timeout_s), _memory_limit(memory_mb):
            exec(compiled, g)  # noqa: S102 — restricted by RestrictedPython
    except _Timeout as exc:
        timed_out = True
        error = f"SandboxTimeout: {exc}"
    except MemoryError as exc:
        error = f"SandboxMemory: {exc}"
    except Exception as exc:
        tb = traceback.format_exception_only(type(exc), exc)
        error = f"SandboxRuntimeError: {''.join(tb).strip()}"
        logger.debug("sandbox runtime error: %s", error)

    # RestrictedPython routes print() through PrintCollector stored in
    # the local `_print` binding. Drain it for the result's stdout field.
    stdout_text = ""
    print_collector = g.get("_print")
    if print_collector is not None:
        try:
            stdout_text = print_collector()  # __call__ joins collected chunks
        except Exception:  # pragma: no cover
            stdout_text = ""

    # Compute namespace delta — exclude RestrictedPython hook names and
    # private (underscore-prefixed) keys.
    reserved = {
        "__builtins__",
        "_getattr_",
        "_getitem_",
        "_getiter_",
        "_iter_unpack_sequence_",
        "_write_",
        "_inplacevar_",
        "_print_",
        "_print",
    }
    delta: dict[str, Any] = {}
    for key, value in g.items():
        if key in reserved:
            continue
        if key.startswith("_"):
            continue
        # Only surface new bindings or changed bindings.
        if key not in pre_keys or namespace.get(key) is not value:
            delta[key] = value

    # Mutate caller namespace in-place with non-error deltas (callers like
    # RLMSession depend on persistence between exec() calls).
    if error is None:
        for k, v in delta.items():
            namespace[k] = v

    return SandboxResult(
        stdout=stdout_text,
        namespace_delta=delta,
        error=error,
        timed_out=timed_out,
    )
