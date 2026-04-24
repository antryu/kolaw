"""
LLM Router — local-first per anthropic_approval_gate memory.

Primary: Qwen3-32B via llama-swap at http://127.0.0.1:8080/v1
Fallback: Claude Sonnet via ANTHROPIC_API_KEY (only if ALLOW_ANTHROPIC=1)

Pattern mirrors y-company llm-router Byzantine design but in Python.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Config from env / defaults
_LOCAL_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
_LOCAL_MODEL = os.getenv("LOCAL_LLM_MODEL", "qwen3:32b")
_ALLOW_ANTHROPIC = os.getenv("ALLOW_ANTHROPIC", "0") == "1"
_ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_TIMEOUT = 60.0


def _dry_run_response(messages: list[dict], **_kwargs) -> str:
    """
    Deterministic mock for dry_run=True.
    Returns a stable string based on message content hash.
    Used in Phase 1 tests to avoid real LLM calls.
    """
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

    Tries local LLM first. Falls back to Anthropic only if:
    - local call fails, AND
    - ALLOW_ANTHROPIC=1 env is set (explicit Andrew approval gate)

    Args:
        messages: OpenAI-format message list
        model: override model name (defaults to LOCAL_LLM_MODEL)
        max_tokens: max output tokens
        temperature: sampling temperature
        dry_run: if True, return deterministic mock without any LLM call

    Returns:
        String content from the assistant turn.
    """
    if dry_run:
        return _dry_run_response(messages)

    # --- Local LLM (primary) ---
    try:
        return await _call_local(messages, model or _LOCAL_MODEL, max_tokens, temperature)
    except Exception as local_exc:
        logger.warning("Local LLM call failed: %s", local_exc)

        if not _ALLOW_ANTHROPIC:
            raise RuntimeError(
                "Local LLM failed and ALLOW_ANTHROPIC is not set. "
                "Set ALLOW_ANTHROPIC=1 after explicit Andrew approval "
                "to enable Claude Sonnet fallback. "
                f"Original error: {local_exc}"
            ) from local_exc

        if not _ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ALLOW_ANTHROPIC=1 but ANTHROPIC_API_KEY is not set."
            ) from local_exc

        logger.warning("Falling back to Anthropic Claude (ALLOW_ANTHROPIC=1)")
        return await _call_anthropic(messages, max_tokens, temperature)


async def _call_local(
    messages: list[dict], model: str, max_tokens: int, temperature: float
) -> str:
    """Call local llama-swap OpenAI-compatible endpoint."""
    url = f"{_LOCAL_BASE_URL}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _call_anthropic(
    messages: list[dict], max_tokens: int, temperature: float
) -> str:
    """Call Anthropic Claude Sonnet. Only reached when ALLOW_ANTHROPIC=1."""
    try:
        import anthropic  # optional dep
    except ImportError as exc:
        raise ImportError(
            "anthropic package not installed. Install it when ALLOW_ANTHROPIC=1 is needed."
        ) from exc

    client = anthropic.AsyncAnthropic(api_key=_ANTHROPIC_API_KEY)
    # Convert OpenAI-format messages: strip system, pass rest
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    user_msgs = [m for m in messages if m["role"] != "system"]
    system_text = "\n\n".join(system_parts) if system_parts else None

    kwargs: dict[str, Any] = {
        "model": "claude-sonnet-4-5",
        "max_tokens": max_tokens,
        "messages": user_msgs,
    }
    if system_text:
        kwargs["system"] = system_text

    response = await client.messages.create(**kwargs)
    return response.content[0].text
