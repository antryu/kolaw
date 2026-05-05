"""
LexGuard MCP client — Phase 4.

Source: github.com/SeoNaRu/lexguard-mcp
Hosted endpoint: https://lexguard-mcp.onrender.com/mcp
License: MIT

LexGuard exposes 18 MCP tools / 159 underlying 법제처 OpenAPIs covering:
  - legal_qa_tool         : unified search (law + precedent + interpretation + ...)
  - precedent_lookup_tool : 판례 (case law)
  - law_article_tool      : specific 조문 lookup
  - interpretation_tool   : 법령해석 (administrative interpretations)
  - constitutional_decision_tool : 헌재 결정
  - administrative_appeal_tool   : 행정심판
  - committee_decision_tool      : 위원회 결정문
  - law_history_tool      : 법령 변경이력
  - + 10 more

The hosted endpoint is reachable without auth, but the underlying 법제처
Open API requires an OC key. Without a valid OC key configured on the
LexGuard server side, tools return empty results (`missing_reason: "NO_MATCH"`)
even for queries that should match.

Two ways to get real data:
  A. Self-host LexGuard with your own LAW_API_KEY  (recommended)
  B. Wait for hosted endpoint to be configured

To self-host:
  git clone https://github.com/SeoNaRu/lexguard-mcp
  cd lexguard-mcp
  LAW_API_KEY=<your_oc_key> docker compose up --build
  → MCP at http://localhost:9099/mcp

To get an OC key (free):
  https://open.law.go.kr/LSO/openApi/guideList.do
  Register → wait ~1 day for approval → copy OC key.

Configure kolaw with:
  LEXGUARD_BASE_URL=http://localhost:9099/mcp     (or hosted URL)
  LEXGUARD_TIMEOUT=30
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = os.getenv("LEXGUARD_BASE_URL", "https://lexguard-mcp.onrender.com/mcp")
_TIMEOUT = float(os.getenv("LEXGUARD_TIMEOUT", "30"))


class LexGuardUnavailable(RuntimeError):
    """Raised when the LexGuard MCP endpoint is unreachable."""


@dataclass
class LexGuardCitation:
    """Normalized result item from LexGuard, regardless of source category."""
    category: str          # law | precedent | interpretation | constitutional | ...
    title: str
    excerpt: str
    case_number: str = ""
    court: str = ""
    decided_at: str = ""
    api_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class LexGuardClient:
    """JSON-RPC 2.0 client over LexGuard's `/mcp` HTTP endpoint."""

    def __init__(self, base_url: str = _BASE_URL, timeout: float = _TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._req_id = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    @staticmethod
    def _parse_sse(text: str) -> dict[str, Any]:
        """LexGuard returns Server-Sent Events frames; strip the 'data: ' prefix."""
        text = text.strip()
        if text.startswith("data:"):
            text = text[5:].lstrip()
        return json.loads(text)

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            payload["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.base_url, json=payload, headers=headers)
                resp.raise_for_status()
                data = self._parse_sse(resp.text)
        except httpx.HTTPError as exc:
            raise LexGuardUnavailable(f"LexGuard call failed at {self.base_url}: {exc}") from exc
        if "error" in data:
            raise LexGuardUnavailable(f"LexGuard returned error: {data['error']}")
        return data.get("result", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._call("tools/list")
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a LexGuard tool by name. Returns the structuredContent."""
        result = await self._call("tools/call", {"name": name, "arguments": arguments})
        return result.get("structuredContent", {})

    # ─── Convenience wrappers (typed args, normalized return) ────────────────

    async def legal_qa(self, query: str, max_per_type: int = 3) -> list[LexGuardCitation]:
        """
        Unified search across all source types.
        Returns a flat list of citations across categories.
        """
        sc = await self.call_tool("legal_qa_tool", {
            "query": query,
            "max_results_per_type": max_per_type,
        })
        out: list[LexGuardCitation] = []
        for category, items in (sc.get("results") or {}).items():
            if not isinstance(items, list):
                continue
            for it in items:
                out.append(_normalize_item(it, category))
        return out

    async def precedent_lookup(self, keyword: str, per_page: int = 5) -> list[LexGuardCitation]:
        sc = await self.call_tool("precedent_lookup_tool", {
            "keyword": keyword,
            "per_page": per_page,
        })
        return [_normalize_item(it, "precedent") for it in sc.get("precedents", [])]

    async def law_article(self, law_name: str, article_number: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"law_name": law_name}
        if article_number:
            args["article_number"] = article_number
        return await self.call_tool("law_article_tool", args)

    async def interpretation(self, query: str, agency: str | None = None, per_page: int = 5) -> list[LexGuardCitation]:
        args: dict[str, Any] = {"query": query, "per_page": per_page}
        if agency:
            args["agency"] = agency
        sc = await self.call_tool("interpretation_tool", args)
        return [_normalize_item(it, "interpretation") for it in sc.get("interpretations", [])]

    async def constitutional_decision(self, query: str, per_page: int = 5) -> list[LexGuardCitation]:
        sc = await self.call_tool("constitutional_decision_tool", {"query": query, "per_page": per_page})
        return [_normalize_item(it, "constitutional") for it in sc.get("decisions", [])]

    async def is_available(self) -> tuple[bool, str]:
        """
        Returns (reachable, status_message).
        Reachable=True with NO_MATCH message means the endpoint works but no
        OC API key is configured upstream — see module docstring.
        """
        try:
            sc = await self.call_tool("health", {})
            api_ready = sc.get("api_ready")
            if api_ready is True:
                return True, "ok"
            return True, sc.get("api_status_message", "reachable, OC key may be missing")
        except LexGuardUnavailable as exc:
            return False, str(exc)


def _normalize_item(item: dict[str, Any], category: str) -> LexGuardCitation:
    """Best-effort normalization across the various LexGuard result shapes."""
    return LexGuardCitation(
        category=category,
        title=item.get("title") or item.get("law_name") or item.get("case_name") or "",
        excerpt=item.get("summary") or item.get("excerpt") or item.get("content") or "",
        case_number=item.get("case_number") or item.get("사건번호") or "",
        court=item.get("court") or item.get("법원") or "",
        decided_at=item.get("decided_at") or item.get("선고일") or "",
        api_url=item.get("api_url") or "",
        raw=item,
    )
