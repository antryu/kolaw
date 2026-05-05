"""
law.go.kr (국가법령정보센터) Open API client.

This is the ground truth source for Korean law that all the higher-level
MCP servers (LexGuard, korean-law-mcp) ultimately wrap. We call it directly
because once you have the `OC` parameter (registered project name at
open.law.go.kr), there's no good reason to add an indirection layer.

The `OC` param is **not a secret** in the cryptographic sense — it's just
the user-chosen identifier you registered with. Treat it like a username,
not a token. Set via `LAW_GO_KR_OC` env var (default: empty → no live calls).

Endpoints used:
  - lawSearch.do?target=law       법령 검색
  - lawSearch.do?target=prec      판례 검색
  - lawSearch.do?target=expc      법령해석례 검색
  - lawSearch.do?target=detc      헌법재판소 결정 검색
  - lawSearch.do?target=decc      행정심판 재결 검색
  - lawSearch.do?target=admrul    행정규칙 검색
  - lawSearch.do?target=ordin     자치법규(조례) 검색
  - lawService.do?target=law      법령 본문 (by MST/ID)
  - lawService.do?target=prec     판례 본문 (by ID)

Reference: https://open.law.go.kr/LSO/openApi/guideList.do
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://www.law.go.kr/DRF"
_OC = os.getenv("LAW_GO_KR_OC", "")
_TIMEOUT = float(os.getenv("LAW_GO_KR_TIMEOUT", "15"))


# Target codes accepted by lawSearch.do
SearchTarget = Literal[
    "law",       # 법령
    "prec",      # 판례
    "expc",      # 법령해석례
    "detc",      # 헌재 결정
    "decc",      # 행정심판 재결
    "admrul",    # 행정규칙
    "ordin",     # 자치법규
]


class LawGoKrUnavailable(RuntimeError):
    """Raised when law.go.kr is unreachable or OC is not configured."""


@dataclass
class LawGoKrItem:
    """One result item, normalized across the various target types."""
    target: str                    # one of SearchTarget values
    item_id: str                   # 법령ID / 판례일련번호 / etc.
    title: str                     # 법령명한글 / 사건명 / 제목
    subtitle: str = ""             # 법령구분명 / 법원명 / 사건번호 / 부처명
    enforced_at: str = ""          # 시행일자 (법령) or 선고일자 (판례)
    detail_url: str = ""           # full URL to the detail endpoint
    raw: dict[str, Any] = field(default_factory=dict)


# Field names per target — law.go.kr returns slightly different shapes
_TARGET_FIELDS = {
    "law": {
        "list_key": "law",
        "id": "법령ID",
        "title": "법령명한글",
        "subtitle": "법령구분명",
        "subtitle2": "소관부처명",
        "date": "시행일자",
        "detail": "법령상세링크",
    },
    "prec": {
        "list_key": "prec",
        "id": "판례일련번호",
        "title": "사건명",
        "subtitle": "법원명",
        "subtitle2": "사건번호",
        "date": "선고일자",
        "detail": "판례상세링크",
    },
    "expc": {
        "list_key": "expc",
        "id": "법령해석례일련번호",
        "title": "안건명",
        "subtitle": "회신기관명",
        "subtitle2": "질의기관명",
        "date": "회신일자",
        "detail": "법령해석례상세링크",
    },
    "ordin": {
        "list_key": "ordin",   # overridden by _LIST_KEY_OVERRIDE
        "id": "자치법규일련번호",
        "title": "자치법규명",
        "subtitle": "지자체기관명",
        "subtitle2": "자치법규분야명",
        "date": "공포일자",
        "detail": "자치법규상세링크",
    },
    "detc": {
        "list_key": "detc",
        "id": "헌재결정례일련번호",
        "title": "사건명",
        "subtitle": "재판부",
        "subtitle2": "사건번호",
        "date": "선고일자",
        "detail": "헌재결정례상세링크",
    },
    "decc": {
        "list_key": "decc",
        "id": "행정심판일련번호",
        "title": "사건명",
        "subtitle": "심판기관명",
        "subtitle2": "사건번호",
        "date": "재결일자",
        "detail": "행정심판상세링크",
    },
    "admrul": {
        "list_key": "admrul",
        "id": "행정규칙일련번호",
        "title": "행정규칙명",
        "subtitle": "행정규칙종류",
        "subtitle2": "소관부처명",
        "date": "발령일자",
        "detail": "행정규칙상세링크",
    },
}

# Top-level wrapper key per target (the JSON envelope from law.go.kr)
# Note: law.go.kr is inconsistent — some targets use "<Target>Search", others
# use just "<Target>". Some targets need separate API authorization (detc =
# 헌재 결정) — those return a schema-only stub with no data array.
_TARGET_WRAPPER = {
    "law": "LawSearch",
    "prec": "PrecSearch",
    "expc": "Expc",
    "detc": "DetcSearch",
    "decc": "Decc",
    "admrul": "AdmRulSearch",
    "ordin": "OrdinSearch",
}

# A few targets diverge: ordin nests its rows under `law` (not `ordin`).
_LIST_KEY_OVERRIDE = {
    "ordin": "law",
}


class LawGoKrClient:
    """Direct HTTP client for law.go.kr DRF endpoints."""

    def __init__(self, oc: str = _OC, timeout: float = _TIMEOUT):
        self.oc = oc
        self.timeout = timeout

    def _ensure_oc(self) -> None:
        if not self.oc:
            raise LawGoKrUnavailable(
                "LAW_GO_KR_OC env var not set. Register at "
                "https://open.law.go.kr/LSO/openApi/guideList.do "
                "(register a project name; that name IS the OC value)."
            )

    async def search(
        self,
        target: SearchTarget,
        query: str,
        display: int = 10,
        page: int = 1,
        extra: dict[str, str] | None = None,
    ) -> list[LawGoKrItem]:
        """Generic search dispatcher."""
        self._ensure_oc()
        params: dict[str, Any] = {
            "target": target,
            "type": "JSON",
            "display": display,
            "page": page,
            "query": query,
            "OC": self.oc,
        }
        if extra:
            params.update(extra)

        url = f"{_BASE}/lawSearch.do"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise LawGoKrUnavailable(f"law.go.kr search failed: {exc}") from exc

        wrapper = _TARGET_WRAPPER[target]
        body = data.get(wrapper, {}) if isinstance(data, dict) else {}
        fields = _TARGET_FIELDS[target]
        list_key = _LIST_KEY_OVERRIDE.get(target, fields["list_key"])
        rows = body.get(list_key, [])
        if isinstance(rows, dict):  # single result is sometimes returned as object, not list
            rows = [rows]
        if not isinstance(rows, list):
            return []

        items: list[LawGoKrItem] = []
        for row in rows:
            sub = row.get(fields["subtitle"], "") or ""
            sub2 = row.get(fields["subtitle2"], "") or ""
            subtitle = " · ".join(s for s in (sub, sub2) if s)
            detail_path = row.get(fields["detail"], "") or ""
            detail_url = (
                f"https://www.law.go.kr{detail_path}"
                if detail_path.startswith("/")
                else detail_path
            )
            items.append(
                LawGoKrItem(
                    target=target,
                    item_id=str(row.get(fields["id"], "") or ""),
                    title=row.get(fields["title"], "") or "",
                    subtitle=subtitle,
                    enforced_at=row.get(fields["date"], "") or "",
                    detail_url=detail_url,
                    raw=row,
                )
            )
        return items

    # Convenience aliases for the common targets
    async def search_law(self, q: str, **kw) -> list[LawGoKrItem]:
        return await self.search("law", q, **kw)

    async def search_precedent(self, q: str, **kw) -> list[LawGoKrItem]:
        return await self.search("prec", q, **kw)

    async def search_interpretation(self, q: str, **kw) -> list[LawGoKrItem]:
        return await self.search("expc", q, **kw)

    async def search_constitutional(self, q: str, **kw) -> list[LawGoKrItem]:
        return await self.search("detc", q, **kw)

    async def search_admin_appeal(self, q: str, **kw) -> list[LawGoKrItem]:
        return await self.search("decc", q, **kw)

    async def search_admin_rule(self, q: str, **kw) -> list[LawGoKrItem]:
        return await self.search("admrul", q, **kw)

    async def search_ordinance(self, q: str, **kw) -> list[LawGoKrItem]:
        return await self.search("ordin", q, **kw)

    async def is_available(self) -> tuple[bool, str]:
        """Probe with a tiny query. (True, 'ok') / (False, reason)."""
        if not self.oc:
            return False, "LAW_GO_KR_OC not configured"
        try:
            items = await self.search("law", "법", display=1)
            if items:
                return True, f"ok (OC={self.oc})"
            return True, f"reachable but no results (OC={self.oc})"
        except LawGoKrUnavailable as exc:
            return False, str(exc)
