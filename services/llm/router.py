"""
LLM Router — Claude OAuth primary per 의장 결재 2026-05-06.

Tier 1 (primary):  Claude via OAuth session (claude CLI) or ANTHROPIC_API_KEY
Tier 2 (fallback): Local Qwen3-32B via llama-swap at http://127.0.0.1:8080/v1
Tier 3 (fallback): DeepSeek V4 (gated by ALLOW_DEEPSEEK=1, OpenAI-compatible)

Rationale:
  Legal domain accuracy requires best-in-class model (의장: "법이라…정확도가 생명").
  Ollama dependency severed — Qwen3 offline was causing local_llm_unavailable errors.
  Claude OAuth session always available when Claude Code is running.

Routing modes:
  - default: Claude OAuth → local llama-swap → DeepSeek (if allowed)
  - KOLAW_PREFER_DEEPSEEK=1: overrides to DeepSeek first (advanced use)

Mode-based rerank routing (Y option):
  - complete_rerank(mode="fast") → Claude OAuth (primary) → local fallback
  - complete_rerank(mode="deep") → Claude OAuth (primary) → local fallback
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Tier 1 — Local llama-swap (m4max at 127.0.0.1 or Tailscale 100.105.53.37)
_LOCAL_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://100.105.53.37:8080/v1")
_LOCAL_MODEL = os.getenv("LOCAL_LLM_MODEL", "Qwen3-32B-Q4_K_M.gguf")

# Tier 2 — DeepSeek V4 (OpenAI-compatible)
_DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
_DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
_DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
_ALLOW_DEEPSEEK = os.getenv("ALLOW_DEEPSEEK", "0") == "1"
_PREFER_DEEPSEEK = os.getenv("KOLAW_PREFER_DEEPSEEK", "0") == "1"

# Tier 1 — Anthropic Claude (always enabled; OAuth session via claude CLI)
_ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Legacy gate — kept for env compatibility but no longer blocks Claude usage
_ALLOW_ANTHROPIC = True

_TIMEOUT = float(os.getenv("KOLAW_LLM_TIMEOUT", "180.0"))


def _dry_run_response(messages: list[dict], **_kwargs) -> str:
    content = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"[dry_run] deterministic mock response hash={digest}"


async def complete(
    messages: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    dry_run: bool = False,
) -> str:
    """
    Send a chat completion request.

    Tier order: Claude OAuth (primary) → local llama-swap → DeepSeek (if ALLOW_DEEPSEEK=1).
    """
    if dry_run:
        return _dry_run_response(messages)

    tiers = _build_tier_order(model)
    last_exc: Exception | None = None
    for tier_name, tier_fn in tiers:
        try:
            return await tier_fn(messages, max_tokens, temperature)
        except Exception as exc:
            last_exc = exc
            logger.warning("LLM tier '%s' failed: %s", tier_name, exc)
            continue

    raise RuntimeError(
        f"All LLM tiers failed. Last error: {last_exc}. "
        "Check ALLOW_DEEPSEEK / ALLOW_ANTHROPIC and corresponding API keys."
    ) from last_exc


def _build_tier_order(model_override: str | None):
    """Return ordered [(name, callable)] of available tiers.

    Tier 1: Claude OAuth (always primary — OAuth session via claude CLI)
    Tier 2: Local llama-swap (fallback when Claude CLI unavailable)
    Tier 3: DeepSeek (fallback if ALLOW_DEEPSEEK=1, after local)
    """
    anthropic_call = lambda msgs, mt, t: _call_anthropic(msgs, mt, t)
    local_call = lambda msgs, mt, t: _call_local(
        msgs, model_override or _LOCAL_MODEL, mt, t
    )
    deepseek_call = lambda msgs, mt, t: _call_deepseek(msgs, mt, t)

    tiers: list[tuple[str, Any]] = []

    # Claude OAuth is always tier 1 (no env flag required)
    tiers.append(("anthropic", anthropic_call))

    # Local llama-swap as fallback
    tiers.append(("local", local_call))

    # DeepSeek as last resort if explicitly enabled
    if _ALLOW_DEEPSEEK and _DEEPSEEK_API_KEY:
        tiers.append(("deepseek", deepseek_call))

    return tiers


async def _call_local(
    messages: list[dict], model: str, max_tokens: int, temperature: float
) -> str:
    url = f"{_LOCAL_BASE_URL}/chat/completions"
    # Qwen3 thinking models suppress the <think> block via /no_think suffix on
    # the last user message. Without it, reasoning_content consumes the token
    # budget and content is empty, causing local_llm_unavailable errors.
    patched_messages = _inject_no_think(messages)
    payload: dict[str, Any] = {
        "model": model,
        "messages": patched_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content.strip():
            raise RuntimeError(
                "Local LLM returned empty content even with /no_think. "
                "Check max_tokens or llama-swap model."
            )
        return content


def _inject_no_think(messages: list[dict]) -> list[dict]:
    """Append /no_think to the last user message if not already present.

    Qwen3 thinking models suppress the <think> block when the user message
    ends with /no_think, putting the answer directly in content rather than
    reasoning_content. This prevents token-budget exhaustion on short calls.
    """
    if not messages:
        return messages
    # Find last user message index
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return messages
    last_content = messages[last_user_idx].get("content", "")
    if "/no_think" in last_content:
        return messages
    patched = list(messages)
    patched[last_user_idx] = dict(patched[last_user_idx])
    patched[last_user_idx]["content"] = last_content + "\n\n/no_think"
    return patched


async def _call_deepseek(
    messages: list[dict], max_tokens: int, temperature: float
) -> str:
    """Call DeepSeek V4 OpenAI-compatible endpoint."""
    if not _DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is empty")

    url = f"{_DEEPSEEK_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {_DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": _DEEPSEEK_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _call_anthropic(
    messages: list[dict], max_tokens: int, temperature: float
) -> str:
    """
    Call Anthropic Claude Opus 4.7.

    Tries in order:
      1. Direct API via anthropic SDK + ANTHROPIC_API_KEY (if key is a valid sk-ant- API key)
      2. Claude Code CLI subprocess (claude -p ...) via OAuth session (always available when CC is running)
    """
    # Build prompt from messages
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    user_msgs = [m for m in messages if m["role"] != "system"]
    system_text = "\n\n".join(system_parts) if system_parts else None

    # Try direct SDK first (only when key looks like a real API key, not OAuth)
    if _ANTHROPIC_API_KEY and not _ANTHROPIC_API_KEY.startswith("sk-ant-"):
        # Non-standard key — skip direct API
        pass
    elif _ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=_ANTHROPIC_API_KEY)
            kwargs: dict[str, Any] = {
                "model": "claude-opus-4-7",
                "max_tokens": max_tokens,
                "messages": user_msgs,
            }
            if system_text:
                kwargs["system"] = system_text
            response = await client.messages.create(**kwargs)
            return response.content[0].text
        except Exception as exc:
            logger.info("Direct SDK call failed (%s) — falling back to CLI", exc)

    # Fallback: Claude Code CLI via OAuth (subprocess)
    return await _call_anthropic_cli(messages, max_tokens)


async def _call_anthropic_cli(
    messages: list[dict],
    max_tokens: int,
) -> str:
    """
    Call Claude Opus 4.7 via `claude -p` CLI (uses Claude Code OAuth session).
    This works when ANTHROPIC_API_KEY is not a valid direct API key but
    the user has an active Claude Code Pro session.
    """
    import asyncio
    import shutil

    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("claude CLI not found in PATH — cannot use OAuth path")

    # Build a single user prompt from messages
    parts = []
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    user_parts = [m["content"] for m in messages if m["role"] == "user"]
    if system_parts:
        parts.append("System: " + " ".join(system_parts))
    parts.extend(user_parts)
    prompt = "\n\n".join(parts)

    # Unset ANTHROPIC_API_KEY so Claude Code uses OAuth session instead of the
    # inherited key (which may be a Claude Code session token, not a direct API key).
    env = {k: v for k, v in __import__("os").environ.items() if k != "ANTHROPIC_API_KEY"}

    proc = await asyncio.create_subprocess_exec(
        claude_bin,
        "-p", prompt,
        "--model", "claude-opus-4-7",
        "--output-format", "text",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {stderr.decode()[:300]}"
        )
    return stdout.decode().strip()


async def complete_rerank(
    messages: list[dict[str, str]],
    mode: str = "fast",
    max_tokens: int = 512,
    temperature: float = 0.1,
) -> str:
    """
    Mode-based LLM call for reranking (Y option).

    mode=fast  → Claude OAuth (primary) → local llama-swap fallback
    mode=deep  → Claude OAuth (primary) → local llama-swap fallback

    Both modes use Claude OAuth by default (2026-05-06 routing change).
    Local llama-swap is preserved as fallback if claude CLI is unavailable.
    """
    # Claude OAuth is always primary for both modes
    try:
        return await _call_anthropic(messages, max_tokens, temperature)
    except Exception as exc:
        logger.warning("Claude rerank failed (%s) — falling back to local", exc)

    # Local llama-swap fallback
    try:
        return await _call_local(messages, _LOCAL_MODEL, max_tokens, temperature)
    except Exception as exc:
        raise RuntimeError(f"All rerank tiers failed. Last error: {exc}") from exc
