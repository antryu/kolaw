"""
korean-law-mcp client — Phase 2.

Source: github.com/chrisryugj/korean-law-mcp (npm: korean-law-mcp@3.5.4)
MCP server with 16 exposed tools covering:
  - 법령(statutes): search_law, get_law_text, get_article_detail
  - 판례(case law): search_decisions, get_decision_text
  - 인용 검증: verify_citations
  - 체인 도구: chain_full_research, chain_impact_analysis, etc.

Investigation outcome (2026-04-24):
- Server runs as stdio MCP server (not HTTP by default)
- Requires 법제처 OC Open API key (OC env var or config)
- MCP HTTP mode: `korean-law-mcp --http --port <N>` but undocumented
- Outcome: (b) BLOCKED — OC API key not available in Andrew's env
  Root cause: 법제처 Open API requires manual registration at open.law.go.kr
  Scaffolding below connects via HTTP once OC key is available.
  Action required: Andrew registers at open.law.go.kr → sets OC_API_KEY env var

Fallback while blocked: all methods raise KolMCPUnavailable with clear message.

To unblock:
  1. Register at https://open.law.go.kr/LSO/openApi/guideList.do
  2. Set env var: KOLMCP_OC_KEY=<your_key>
  3. Start server: korean-law-mcp --http --port 3001
  4. Set env var: KOLMCP_BASE_URL=http://localhost:3001
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = os.getenv("KOLMCP_BASE_URL", "http://localhost:3001")
_OC_KEY = os.getenv("KOLMCP_OC_KEY", "")
_TIMEOUT = 30.0


class KolMCPUnavailable(RuntimeError):
    """Raised when korean-law-mcp server is not reachable or not configured."""


def _check_configured() -> None:
    """Raise if the MCP server is not configured."""
    if not _OC_KEY:
        raise KolMCPUnavailable(
            "KOLMCP_OC_KEY not set. "
            "Register at https://open.law.go.kr/LSO/openApi/guideList.do "
            "then set KOLMCP_OC_KEY=<your_key> and KOLMCP_BASE_URL=http://localhost:3001. "
            "See services/data/kolmcp_client.py for setup steps."
        )


class KolMCPClient:
    """
    HTTP client for korean-law-mcp MCP server.

    Phase 2: HTTP mode scaffolding. Requires OC API key + running server.
    Methods call the MCP server's REST-compatible endpoints.

    Phase 3: Replace with full MCP SDK stdio client for richer tool access.
    """

    def __init__(self, base_url: str = _BASE_URL, oc_key: str = _OC_KEY):
        self.base_url = base_url.rstrip("/")
        self.oc_key = oc_key

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    async def _post(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Send a tool call to the MCP server."""
        _check_configured()
        url = f"{self.base_url}/tools/{tool}"
        payload = {"arguments": args}
        if self.oc_key:
            payload["oc"] = self.oc_key
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                resp = await client.post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
                return resp.json()
            except httpx.ConnectError as exc:
                raise KolMCPUnavailable(
                    f"korean-law-mcp server not reachable at {self.base_url}. "
                    "Start with: korean-law-mcp --http --port 3001"
                ) from exc

    async def search_law(self, query: str, limit: int = 10) -> list[dict]:
        """Search statutes via korean-law-mcp search_law tool."""
        result = await self._post("search_law", {"query": query, "display": limit})
        return result.get("results", [])

    async def get_law_text(self, law_id: str) -> dict:
        """Get full statute text via get_law_text tool."""
        return await self._post("get_law_text", {"law_id": law_id})

    async def search_decisions(
        self, query: str, domain: str = "precedent", limit: int = 10
    ) -> list[dict]:
        """Search case law (판례) via search_decisions tool."""
        result = await self._post(
            "search_decisions", {"query": query, "domain": domain, "display": limit}
        )
        return result.get("results", [])

    async def get_decision_text(self, decision_id: str, domain: str = "precedent") -> dict:
        """Get full decision text via get_decision_text tool."""
        return await self._post(
            "get_decision_text", {"id": decision_id, "domain": domain}
        )

    async def verify_citations(self, text: str) -> dict:
        """Verify law citations in text against 법제처 DB."""
        return await self._post("verify_citations", {"text": text})

    async def chain_full_research(self, query: str) -> dict:
        """Run chain_full_research for comprehensive legal analysis."""
        return await self._post("chain_full_research", {"query": query})

    async def is_available(self) -> bool:
        """Check if MCP server is reachable (health probe)."""
        if not self.oc_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/health")
                return resp.status_code == 200
        except Exception:
            return False
