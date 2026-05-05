"""
Fast search path — keyword grep + law.go.kr direct API.

Phase 1: ChromaDB-only (fixture data).
Phase 2: keyword grep over legalize-kr (primary).
Phase 3: law.go.kr Open API direct (enrichment with live precedent / 해석 /
         행정규칙 / 조례 — anything legalize-kr's offline statute corpus
         doesn't cover).

Returns structured Citations compatible with kolaw SearchResponse.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from apps.api.schemas import Citation, SearchRequest, SearchResponse
from services.data.data_go_kr_court import (
    CourtPrecedent,
    DataGoKrCourtClient,
    DataGoKrCourtUnavailable,
    is_constitution_query,
)
from services.data.law_go_kr import LawGoKrClient, LawGoKrItem, LawGoKrUnavailable
from services.data.legalize_kr import GrepHit, grep_search

logger = logging.getLogger(__name__)

_GREP_LIMIT = 10
_LAW_GO_KR_LIMIT = 5


def _grep_to_citation(hit: GrepHit) -> Citation:
    """Convert a legalize-kr grep hit to a Citation."""
    excerpt_lines = [ln for ln in hit.excerpt.split("\n") if ln.strip()]
    body_lines: list[str] = []
    for ln in excerpt_lines:
        # git grep -n format: "<file>:<lineno>:<text>" or "<file>-<lineno>-<text>"
        m = re.match(r"^[^:]+[-:](\d+)[-:](.*)$", ln)
        if m:
            body_lines.append(m.group(2))
        else:
            body_lines.append(ln)
    excerpt = " · ".join(line.strip() for line in body_lines if line.strip())[:400]

    return Citation(
        law_id=hit.law_name,           # legalize-kr uses folder name as identifier
        law_name=hit.law_name,
        article=hit.type or "법령",
        version="",                     # populated in Phase 4 from frontmatter
        excerpt=excerpt,
    )


def _law_go_kr_to_citation(item: LawGoKrItem) -> Citation:
    """Convert a law.go.kr search result to a Citation."""
    article_label = {
        "law": "법령",
        "prec": "판례",
        "expc": "법령해석",
        "detc": "헌재결정",
        "decc": "행정심판",
        "admrul": "행정규칙",
        "ordin": "조례",
    }.get(item.target, item.target)
    excerpt = item.subtitle if item.subtitle else item.detail_url
    return Citation(
        law_id=item.item_id or item.title,
        law_name=item.title,
        article=article_label,
        version=item.enforced_at.replace(".", "").replace("-", ""),
        excerpt=excerpt[:400],
    )


def _court_to_citation(p: CourtPrecedent) -> Citation:
    """Convert a 헌법재판소 decision row to a Citation."""
    excerpt_bits = [p.class_nm or "헌법재판소 결정", p.adjudge_dt or ""]
    return Citation(
        law_id=p.event_no or p.seq,
        law_name=p.nick or p.title or p.event_no,
        article="헌재결정",
        version=(p.adjudge_dt or "").replace(".", "").replace("-", ""),
        excerpt=" · ".join(b for b in excerpt_bits if b)[:400],
    )


async def _data_go_kr_court_enrichment(query: str) -> list[Citation]:
    """
    Optional 헌재 enrichment via data.go.kr.

    Only runs when the query mentions a constitution-related keyword
    (헌법, 헌재, 위헌, 합헌, 기본권, 탄핵, etc.) to conserve the 1000/day
    rate limit. Returns [] if the API is not configured or fails.
    """
    if not is_constitution_query(query):
        return []
    if not os.getenv("DATA_GO_KR_KEY"):
        return []

    # eventNm filter is a literal substring match against the case nickname,
    # so multi-word queries rarely hit. Strip constitution-marker words and
    # try the remaining first keyword. Fall back to no filter if nothing left.
    _STOPWORDS = {"헌법", "헌재", "위헌", "합헌", "헌법불합치", "한정합헌",
                  "한정위헌", "헌법소원", "기본권", "탄핵", "정당해산", "권한쟁의"}
    keywords = [w for w in (query or "").split() if w and w not in _STOPWORDS]
    refined = keywords[0] if keywords else None

    try:
        client = DataGoKrCourtClient()
        items = await client.search_realm(query=refined, category=0, per_page=5)
    except DataGoKrCourtUnavailable as exc:
        logger.warning("data.go.kr 헌재 enrichment skipped: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("data.go.kr 헌재 enrichment exception: %s", exc)
        return []
    return [_court_to_citation(p) for p in items]


async def _law_go_kr_enrichment(query: str) -> list[Citation]:
    """
    Optional enrichment from law.go.kr Open API. Returns [] if OC not configured
    or all calls fail. Fetches 법령 + 판례 + 해석 in parallel.
    """
    if not os.getenv("LAW_GO_KR_OC"):
        return []
    client = LawGoKrClient()
    targets = [
        client.search_law(query, display=_LAW_GO_KR_LIMIT),
        client.search_precedent(query, display=_LAW_GO_KR_LIMIT),
        client.search_interpretation(query, display=_LAW_GO_KR_LIMIT),
    ]
    citations: list[Citation] = []
    results = await asyncio.gather(*targets, return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            if not isinstance(r, LawGoKrUnavailable):
                logger.warning("law.go.kr enrichment exception: %s", r)
            continue
        for item in r:
            citations.append(_law_go_kr_to_citation(item))
    return citations


def _dedupe_citations(citations: list[Citation]) -> list[Citation]:
    """Drop duplicates by (law_name, article)."""
    seen: set[tuple[str, str]] = set()
    out: list[Citation] = []
    for c in citations:
        key = (c.law_name.strip(), c.article.strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _confidence_from_hits(hit_count: int, mode: str) -> float:
    """Heuristic: more hits → higher confidence, capped."""
    if hit_count == 0:
        return 0.0
    if mode == "OR":
        # OR widens the set — same count means less specificity
        return min(0.5 + 0.05 * hit_count, 0.85)
    return min(0.6 + 0.04 * hit_count, 0.95)


async def fast_search(req: SearchRequest) -> SearchResponse:
    """
    Phase 2 fast path:
      1. legalize-kr keyword grep (primary, fast, exact)
      2. (Future) ChromaDB vector for semantic widening
      3. Merge + dedupe + rank
    """
    try:
        result = await grep_search(req.query, limit=_GREP_LIMIT)
    except Exception as exc:  # noqa: BLE001
        logger.exception("grep_search failed")
        return SearchResponse(
            verdict="ambiguous",
            confidence=0.0,
            citations=[],
            trajectory_id=None,
            mode="fast",
            error=f"grep_search exception: {exc}",
        )

    if result.error:
        return SearchResponse(
            verdict="ambiguous",
            confidence=0.0,
            citations=[],
            trajectory_id=None,
            mode="fast",
            error=result.error,
        )

    grep_citations = [_grep_to_citation(h) for h in result.hits]
    law_go_kr_citations, court_citations = await asyncio.gather(
        _law_go_kr_enrichment(req.query),
        _data_go_kr_court_enrichment(req.query),
    )
    citations = _dedupe_citations(grep_citations + law_go_kr_citations + court_citations)
    confidence = _confidence_from_hits(len(citations), result.mode)

    if confidence >= 0.7:
        verdict = "applies"
    elif confidence >= 0.4:
        verdict = "ambiguous"
    else:
        verdict = "does_not_apply"

    return SearchResponse(
        verdict=verdict,
        confidence=round(confidence, 3),
        citations=citations,
        trajectory_id=None,
        mode="fast",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Backward-compat shims for ChromaDB-era tests
#
# Phase 1 used ChromaDB as the fast path; Phase 3 retired it in favor of
# legalize-kr grep + law.go.kr live API. The Phase 1 tests (test_deep_mock,
# test_ingest_legalize_kr, test_rlm_minimal_loop) patch `_get_collection`
# from this module. We keep a stub here so those imports don't crash.
# Tests marked `requires_chroma` skip when this stub is in effect.
# ─────────────────────────────────────────────────────────────────────────────


_CHROMA_RETIRED = True


def _get_collection():  # pragma: no cover — retained for legacy test imports only
    """Retired in Phase 3. Use grep_search() + LawGoKrClient instead."""
    raise NotImplementedError(
        "ChromaDB fast path retired in Phase 3. "
        "Use services.data.legalize_kr.grep_search and "
        "services.data.law_go_kr.LawGoKrClient instead."
    )
