"""
korean-law-mcp client.

Source: https://github.com/chrisryugj/korean-law-mcp
Wraps 법제처 Open API behind 16 MCP tools (16 of 41 endpoints) plus
LLM citation hallucination verification.

Why is this even here when kolaw already calls law.go.kr directly?
chrisryugj's primary value-add is `verify_citations` and the chain
research tools (`chain_full_research`, `chain_impact_analysis`) — these
do multi-step reasoning that the raw OpenAPI doesn't.

Setup (caller-side, ~5 minutes once you have an OC):

    npm install -g korean-law-mcp
    LAW_OC=$LAW_GO_KR_OC korean-law-mcp --http --port 3001 &

Then either:
  - export KOLMCP_BASE_URL=http://localhost:3001
  - or rely on the default

By default this client reuses `LAW_GO_KR_OC` so kolaw users only need
to register once at open.law.go.kr.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = os.getenv("KOLMCP_BASE_URL", "http://localhost:3001")
# Reuse the same OC the rest of kolaw uses; allow KOLMCP_OC override if
# the caller wants a different identity for chrisryugj specifically.
_OC = os.getenv("KOLMCP_OC") or os.getenv("LAW_GO_KR_OC", "")
_TIMEOUT = float(os.getenv("KOLMCP_TIMEOUT", "30"))


class KolMCPUnavailable(RuntimeError):
    """Raised when korean-law-mcp server is not reachable or not configured."""


class KolMCPClient:
    """
    HTTP client for a locally-running korean-law-mcp server.

    Each method maps 1:1 to a chrisryugj tool name. The MCP server speaks
    JSON-RPC 2.0 over a `/mcp` endpoint when started with `--http`. We
    accept both the JSON-RPC envelope and the older `/tools/<name>`
    REST shape some forks expose.
    """

    def __init__(self, base_url: str = _BASE_URL, oc: str = _OC, timeout: float = _TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.oc = oc
        self.timeout = timeout
        self._req_id = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _ensure_configured(self) -> None:
        if not self.oc:
            raise KolMCPUnavailable(
                "No OC configured. Set LAW_GO_KR_OC (or KOLMCP_OC) to your "
                "open.law.go.kr registered project name. "
                "See https://open.law.go.kr/LSO/openApi/guideList.do"
            )

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Invoke an MCP tool. Tries JSON-RPC first, falls back to REST."""
        self._ensure_configured()
        rpc_url = f"{self.base_url}/mcp"
        rest_url = f"{self.base_url}/tools/{tool}"

        # Inject OC if the caller didn't already
        args = {**args}
        if "oc" not in args:
            args["oc"] = self.oc

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # Try JSON-RPC 2.0 first
            try:
                resp = await client.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": self._next_id(),
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": args},
                    },
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                )
                if resp.status_code == 200:
                    text = resp.text.strip()
                    if text.startswith("data:"):
                        text = text[5:].lstrip()
                    import json
                    data = json.loads(text)
                    if "error" in data:
                        raise KolMCPUnavailable(f"MCP error: {data['error']}")
                    result = data.get("result", {})
                    return result.get("structuredContent", result)
            except httpx.ConnectError as exc:
                raise KolMCPUnavailable(
                    f"korean-law-mcp not reachable at {self.base_url}. "
                    "Run: LAW_OC=$LAW_GO_KR_OC korean-law-mcp --http --port 3001"
                ) from exc
            except httpx.HTTPError as exc:
                logger.debug("JSON-RPC call failed (%s); falling back to REST", exc)

            # Fallback: legacy REST shape
            try:
                resp = await client.post(rest_url, json={"arguments": args})
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as exc:
                raise KolMCPUnavailable(f"both /mcp and /tools/{tool} failed: {exc}") from exc

    # ─── Convenience wrappers (1:1 with chrisryugj tool names) ───────────────

    async def search_law(self, query: str, limit: int = 10) -> list[dict]:
        result = await self._call("search_law", {"query": query, "display": limit})
        return result.get("results", []) or result.get("law", [])

    async def get_law_text(self, law_id: str) -> dict[str, Any]:
        return await self._call("get_law_text", {"law_id": law_id})

    async def search_decisions(
        self, query: str, domain: str = "precedent", limit: int = 10
    ) -> list[dict]:
        result = await self._call(
            "search_decisions",
            {"query": query, "domain": domain, "display": limit},
        )
        return result.get("results", []) or result.get("decisions", [])

    async def get_decision_text(self, decision_id: str, domain: str = "precedent") -> dict[str, Any]:
        return await self._call(
            "get_decision_text",
            {"id": decision_id, "domain": domain},
        )

    async def verify_citations(self, text: str) -> dict[str, Any]:
        """The headline tool: cross-validate citations in `text` against 법제처 DB."""
        return await self._call("verify_citations", {"text": text})

    async def chain_full_research(self, query: str) -> dict[str, Any]:
        """Multi-step orchestration: law → precedent → interpretation → impact."""
        return await self._call("chain_full_research", {"query": query})

    async def is_available(self) -> tuple[bool, str]:
        """
        Returns (reachable, message).

        Probes via MCP `tools/list` and checks for a chrisryugj-known tool name —
        a plain GET /health passes for any service that happens to listen on the
        same port (we hit a stray websocket broker on 3001 that did exactly that).
        """
        if not self.oc:
            return False, "OC not configured (set LAW_GO_KR_OC or KOLMCP_OC)"
        try:
            import json
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self.base_url}/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                )
            if resp.status_code != 200:
                return False, f"not chrisryugj — /mcp returned {resp.status_code}"
            text = resp.text.strip()
            if text.startswith("data:"):
                text = text[5:].lstrip()
            data = json.loads(text)
            tools = (data.get("result") or {}).get("tools", []) or []
            tool_names = {t.get("name") for t in tools if isinstance(t, dict)}
            # chrisryugj's signature tool — confirms identity
            if "verify_citations" in tool_names or "search_law" in tool_names:
                return True, f"ok ({len(tool_names)} tools at {self.base_url})"
            return False, "service responded but does not look like korean-law-mcp"
        except httpx.ConnectError:
            return False, f"not running at {self.base_url} — npm install -g korean-law-mcp"
        except Exception as exc:  # noqa: BLE001
            return False, f"probe failed: {exc}"
