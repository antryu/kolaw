"""
data.go.kr 헌법재판소 판례 API client.

The other client in this package — services/data/law_go_kr.py — talks to the
법제처 OpenAPI at law.go.kr/DRF and gets 6 of 7 supported targets out of the
box. The 7th target (`detc`, 헌법재판소 결정) requires a separate permission
request that takes a business day to approve.

This client uses a different API path entirely: data.go.kr's
PrecedentInfomationService, which exposes 헌재 결정문 directly with a
real serviceKey. Approval is automatic on registration, and 1000
requests/day per endpoint are available without further gating.

Two endpoints are exposed:
  /getOcprPrcdntList     — 헌법재판소 공보 (monthly bulletin PDF list)
  /getRealmMainPrcdntList — 분야별 주요 판례 (individual decisions, useful)

We only wrap the second endpoint (`getRealmMainPrcdntList`) because it
returns individual decisions with case numbers (`2020헌마956` style)
that align with how the rest of kolaw treats citations. The first endpoint
returns monthly bulletin PDFs — useful for downloads but not for granular
search.

Category codes (`code` parameter):
  0 — 전체
  1 — 정치 · 선거관계에 관한 결정
  3 — 언론 등 정신적 자유에 관한 결정
  4 — 경제 · 재산권 · 조세관계에 관한 결정
  5 — 가족 · 노동 등 사회관계에 관한 결정
  6 — 절차적 기본권 및 형사관계에 관한 결정
  7 — 헌법위원회 및 대법원 헌법판례

(Note: code=2 doesn't exist in the spec — likely intentional.)

Configure with:
  DATA_GO_KR_KEY=<decoded serviceKey from data.go.kr profile>
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://apis.data.go.kr/9750000/PrecedentInfomationService"
_KEY = os.getenv("DATA_GO_KR_KEY", "")
_TIMEOUT = float(os.getenv("DATA_GO_KR_TIMEOUT", "15"))

# Category code → human label
CATEGORIES: dict[int, str] = {
    0: "전체",
    1: "정치·선거",
    3: "언론·정신적자유",
    4: "경제·재산권·조세",
    5: "가족·노동·사회관계",
    6: "절차적기본권·형사",
    7: "헌법위원회·대법원헌법판례",
}


@dataclass
class CourtPrecedent:
    """One 헌법재판소 decision row, normalized for kolaw's citation shape."""
    seq: str                # internal sequence id (4264372 etc.)
    nick: str               # case nickname (e.g. "비례대표국회의원 의석할당 사건")
    title: str              # formal name (e.g. "공직선거법 제189조 제1항 제1호 위헌확인")
    event_no: str           # case number (e.g. "2020헌마956")
    class_nm: str           # category label
    adjudge_dt: str         # decision date YYYYMMDD
    reg_date: str = ""      # registration date YYYYMMDD
    raw: dict[str, Any] = field(default_factory=dict)


class DataGoKrCourtUnavailable(RuntimeError):
    """Raised when the data.go.kr 헌재 API is unreachable or unconfigured."""


class DataGoKrCourtClient:
    """REST client for data.go.kr's 헌법재판소 판례 service."""

    def __init__(self, key: str = _KEY, timeout: float = _TIMEOUT):
        self.key = key
        self.timeout = timeout

    def _ensure_key(self) -> None:
        if not self.key:
            raise DataGoKrCourtUnavailable(
                "DATA_GO_KR_KEY not set. Apply at https://www.data.go.kr → "
                "검색 '헌법재판소 판례정보' → 활용신청 (auto-approved). "
                "Then add the Decoding key as DATA_GO_KR_KEY=... in .env."
            )

    async def search_realm(
        self,
        query: str | None = None,
        category: int = 0,
        page: int = 1,
        per_page: int = 10,
    ) -> list[CourtPrecedent]:
        """
        Search 분야별 주요 판례 (the useful endpoint).

        Args:
          query    — keyword to match against case nicknames (eventNm filter).
                     If None, returns all decisions in the category.
          category — code 0..7 (see CATEGORIES dict). 0 = all.

        Returns CourtPrecedent list (may be empty).
        """
        self._ensure_key()
        params: dict[str, Any] = {
            "serviceKey": self.key,
            "pageNo": page,
            "numOfRows": per_page,
            "type": "json",
            "code": category,
        }
        if query:
            params["eventNm"] = query

        url = f"{_BASE}/getRealmMainPrcdntList"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise DataGoKrCourtUnavailable(f"data.go.kr 헌재 API failed: {exc}") from exc

        header = (data.get("header") or {})
        if header.get("resultCode") not in (None, "0", "00"):
            raise DataGoKrCourtUnavailable(
                f"data.go.kr 헌재 API error: {header.get('resultMsg', 'unknown')}"
            )

        body = data.get("body") or {}
        items_wrap = body.get("items") or {}
        rows = items_wrap.get("item", []) if isinstance(items_wrap, dict) else []
        if isinstance(rows, dict):
            rows = [rows]

        return [
            CourtPrecedent(
                seq=str(r.get("seq", "")),
                nick=r.get("nick", "") or "",
                title=r.get("title", "") or "",
                event_no=r.get("eventNo", "") or "",
                class_nm=r.get("classNm", "") or "",
                adjudge_dt=r.get("adjudgeDt", "") or "",
                reg_date=r.get("regDate", "") or "",
                raw=r,
            )
            for r in rows
        ]

    async def is_available(self) -> tuple[bool, str]:
        """Probe with a tiny request. Returns (reachable, message)."""
        if not self.key:
            return False, "DATA_GO_KR_KEY not configured"
        try:
            items = await self.search_realm(category=0, per_page=1)
            return True, f"ok ({len(items)} item probe)"
        except DataGoKrCourtUnavailable as exc:
            return False, str(exc)


# ─── Smart query routing ─────────────────────────────────────────────────────

_CONSTITUTION_KEYWORDS = (
    "헌법", "헌재", "위헌", "합헌", "헌법불합치", "한정합헌", "한정위헌",
    "헌법소원", "기본권", "탄핵", "정당해산", "권한쟁의",
)


def is_constitution_query(query: str) -> bool:
    """Cheap heuristic: should we hit the 헌재 API for this query?"""
    q = query or ""
    return any(kw in q for kw in _CONSTITUTION_KEYWORDS)
