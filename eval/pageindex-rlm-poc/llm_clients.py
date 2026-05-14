"""
llm_clients.py — Claude CLI (max plan) + Qwen3-32B (llama-swap) 호출 wrapper.

Claude API key 가 invalid 라 Claude Code CLI subprocess 로 호출 (의장 max plan 인증).
이 방법은 anthropic_approval_gate 자연 통과 — pay-per-token 발생 X (max plan 정액).

Cost monitoring: Claude max plan 은 토큰 단가 X, 그래도 응답 길이 (chars) 기록.
Qwen3-32B llama-swap: 무료 (local).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

LLAMA_SWAP_URL = os.environ.get("LLAMA_SWAP_URL", "http://127.0.0.1:8080/v1/chat/completions")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/opt/homebrew/bin/claude")


@dataclass
class CallRecord:
    model: str
    role: str  # "answer" / "critique" / "revise"
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    latency_ms: int = 0
    text: str = ""
    error: Optional[str] = None


@dataclass
class CostLedger:
    calls: list[CallRecord] = field(default_factory=list)

    def total_usd(self) -> float:
        return round(sum(c.usd for c in self.calls), 4)

    def by_model(self) -> dict[str, dict]:
        agg: dict[str, dict] = {}
        for c in self.calls:
            a = agg.setdefault(c.model, {"in": 0, "out": 0, "usd": 0.0, "calls": 0})
            a["in"] += c.input_tokens
            a["out"] += c.output_tokens
            a["usd"] += c.usd
            a["calls"] += 1
        for v in agg.values():
            v["usd"] = round(v["usd"], 4)
        return agg


def call_claude(
    prompt: str,
    system: str = "",
    model: str = "claude-opus-4-5",
    max_tokens: int = 2048,
    role: str = "answer",
) -> CallRecord:
    """
    Invoke Claude via Claude Code CLI (`claude -p`).

    의장 max plan 인증 사용 — pay-per-token X.
    텍스트 prompt + optional system 메시지 → stdout text 반환.
    """
    full_prompt = prompt if not system else f"[SYSTEM]\n{system}\n[USER]\n{prompt}"
    cmd = [CLAUDE_BIN, "-p", "--model", model, full_prompt]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            stdin=subprocess.DEVNULL,  # never block on stdin (e.g. nohup, headless)
        )
    except subprocess.TimeoutExpired:
        return CallRecord(
            model=model,
            role=role,
            error="timeout 600s",
            latency_ms=600000,
        )
    except Exception as e:
        return CallRecord(
            model=model,
            role=role,
            error=f"{type(e).__name__}: {e}",
            latency_ms=int((time.time() - t0) * 1000),
        )

    if proc.returncode != 0:
        return CallRecord(
            model=model,
            role=role,
            error=f"exit {proc.returncode}: {proc.stderr[:500]}",
            latency_ms=int((time.time() - t0) * 1000),
        )

    text = proc.stdout.strip()
    # max plan: no per-token billing, approximate input/output by char count
    in_chars = len(full_prompt)
    out_chars = len(text)
    return CallRecord(
        model=model,
        role=role,
        input_tokens=in_chars // 4,  # rough char→token proxy
        output_tokens=out_chars // 4,
        usd=0.0,
        latency_ms=int((time.time() - t0) * 1000),
        text=text,
    )


def call_qwen(
    prompt: str,
    system: str = "",
    model: str = "Qwen3-32B-Q4_K_M.gguf",
    max_tokens: int = 1024,
    role: str = "critique",
    no_think: bool = True,
) -> CallRecord:
    """
    Call llama-swap OpenAI-compatible API for local Qwen3-32B.
    Free (local), so usd = 0.

    Qwen3 default 는 <think>...</think> reasoning block 출력. PoC 는 직접 비판
    텍스트만 필요 → system prompt 에 `/no_think` prefix 로 차단.
    """
    sys_msg = system or "You are a careful Korean legal critic."
    if no_think and "/no_think" not in sys_msg:
        sys_msg = "/no_think " + sys_msg

    msgs = [{"role": "system", "content": sys_msg}]
    msgs.append({"role": "user", "content": prompt})

    body = {
        "model": model,
        "messages": msgs,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "stream": False,
    }
    req = urllib.request.Request(
        LLAMA_SWAP_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return CallRecord(
            model=model,
            role=role,
            error=f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}",
            latency_ms=int((time.time() - t0) * 1000),
        )
    except Exception as e:
        return CallRecord(
            model=model,
            role=role,
            error=f"{type(e).__name__}: {e}",
            latency_ms=int((time.time() - t0) * 1000),
        )

    usage = data.get("usage", {}) or {}
    text = ""
    if data.get("choices"):
        msg = data["choices"][0].get("message", {})
        text = msg.get("content", "") or ""
    return CallRecord(
        model=model,
        role=role,
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        usd=0.0,
        latency_ms=int((time.time() - t0) * 1000),
        text=text,
    )
