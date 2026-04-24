"""
korean-law-mcp client stub.

Source: github.com/chrisryugj/korean-law-mcp
MCP server with 64 tools covering:
  - 법령(statutes): search, detail, article
  - 판례(case law): constitutional court, supreme court
  - 행정규칙(administrative rules): ministry circulars
  - 조례(municipal ordinances): regional regulations
  - 헌법재판소(Constitutional Court): decisions
  - 조세심판(tax tribunal): rulings
  - 관세(customs): tariff schedules

Phase 1: stub only — tools are documented, no live MCP calls.
Phase 2: wire actual MCP client using the mcp Python SDK.

TODO (Phase 2 — wire these 64 tools):
  Statutes (법령):
    - law_search: keyword search across all statutes
    - law_detail: full text by law_id
    - law_article: single article by law_id + article_no
    - law_history: amendment timeline for a law
    - law_related: find related statutes by xref

  Case law (판례):
    - precedent_search: search court decisions
    - precedent_detail: full decision text
    - constitutional_search: 헌법재판소 decisions
    - constitutional_detail: full 헌재 decision

  Administrative rules (행정규칙):
    - admin_rule_search: search ministry circulars
    - admin_rule_detail: full circular text

  Municipal ordinances (조례):
    - ordinance_search: search by region + keyword
    - ordinance_detail: full ordinance text

  Tax tribunal (조세심판):
    - tax_tribunal_search: search rulings
    - tax_tribunal_detail: full ruling text

  Customs (관세):
    - customs_search: tariff schedule search
    - customs_detail: specific tariff code detail

  (remaining tools follow same search+detail pattern per domain)
"""

from __future__ import annotations


class KolMCPClient:
    """
    Stub client for korean-law-mcp MCP server.

    Phase 1: all methods return NotImplementedError with clear Phase 2 note.
    Phase 2: replace with actual MCP SDK calls.
    """

    def __init__(self, server_url: str = "http://localhost:3001"):
        self.server_url = server_url

    def law_search(self, query: str, limit: int = 10) -> list[dict]:
        raise NotImplementedError(
            "kolmcp_client.law_search: Phase 2. "
            "Wire MCP call to korean-law-mcp server at self.server_url."
        )

    def precedent_search(self, query: str, court: str = "all", limit: int = 10) -> list[dict]:
        raise NotImplementedError(
            "kolmcp_client.precedent_search: Phase 2."
        )

    def constitutional_search(self, query: str, limit: int = 10) -> list[dict]:
        raise NotImplementedError(
            "kolmcp_client.constitutional_search: Phase 2."
        )
