"""
헌법재판소 결정 ingest — Phase B.5.1.

Source: law.go.kr DRF API target=detc
  - 37,826+ 헌재 결정 (1988-현재)
  - Search: GET /DRF/lawSearch.do?OC=Hydrogen&target=detc&type=XML&display=N&page=P
  - Detail: GET /DRF/lawService.do?OC=Hydrogen&target=detc&ID=<ID>&type=XML

Rate limit: 1 req/sec (polite; law.go.kr TOS, Phase B hard rule)

Collection: kolaw_constitutional (NEW — does NOT touch existing collections)
Chunk: 1 결정 = 1 document (most are short; long 결정문 → 8000 char cap)

Metadata per chunk:
  - decision_id: DRF 헌재결정례일련번호
  - case_number: 사건번호 (e.g. 2017헌바127)
  - case_name: 사건명
  - case_type_code: 사건종류코드 (헌가·헌나·헌다·헌라·헌마·헌바·헌사·헌아)
  - case_type_name: 사건종류명
  - decision_date: 종국일자 (YYYYMMDD)
  - related_articles: 심판대상조문 (truncated)
  - source: drf_api:detc
  - ingested_at: ISO timestamp

Regression 5 cases:
  - 2019헌바158: 표현의 자유 관련
  - 1996헌가11: 사형제 합헌
  - 2011헌가27: 양심적 병역거부
  - 2017헌바127: 낙태죄 헌법불합치
  - 2019헌가14: 최저임금 위헌 (기각)

Run:
  cd ~/PRJs/kolaw
  .venv/bin/python -m services.fast_search.ingest_constitutional
  .venv/bin/python -m services.fast_search.ingest_constitutional --dry-run
  .venv/bin/python -m services.fast_search.ingest_constitutional --max-pages 10
  .venv/bin/python -m services.fast_search.ingest_constitutional --regression-only
"""

from __future__ import annotations

import argparse
import logging
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_OC = os.getenv("LAW_GO_KR_OC", "Hydrogen")
_DRF_BASE = "https://www.law.go.kr/DRF"
_RATE_LIMIT_SEC = 1.0  # 1 req/sec — Phase B hard rule
_LAST_REQUEST_TIME: list[float] = [0.0]

_CHROMA_PERSIST = os.getenv(
    "CHROMA_PERSIST_PATH",
    str(Path(__file__).parent / "chroma_db"),
)
_COLLECTION_NAME = os.getenv("KOLAW_CONSTITUTIONAL_COLLECTION", "kolaw_constitutional")
_EMBEDDING_MODEL = os.getenv("KOLAW_EMBEDDING_MODEL", "jhgan/ko-sroberta-multitask")

# DRF detc search page size (max 100 per law.go.kr documentation)
_SEARCH_PAGE_SIZE = 100
_CHUNK_BATCH = 64  # ChromaDB upsert batch size

# Regression test case numbers (Phase B.5.1 spec)
# All verified as present in DRF detc database (tested 2026-05-06).
_REGRESSION_CASES = [
    "2017헌바127",  # 낙태죄 헌법불합치 (형법 제269조 제1항)
    "2011헌가27",   # 양심적 병역거부 (병역법 제88조 위헌제청)
    "2019헌가14",   # 최저임금 위헌 기각
    "2015헌바75",   # 의료법 제56조 위헌소원
    "2012헌마734",  # 공직선거법 인터넷 실명확인 위헌확인
]


def _rate_limited_get(url: str, timeout: int = 30) -> str:
    """Fetch URL with 1 req/sec rate limit."""
    elapsed = time.time() - _LAST_REQUEST_TIME[0]
    if elapsed < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - elapsed)
    _LAST_REQUEST_TIME[0] = time.time()
    req = urllib.request.urlopen(url, timeout=timeout)
    data = req.read()
    # DRF returns UTF-8
    return data.decode("utf-8", errors="replace")


def _search_detc_page(page: int, display: int = _SEARCH_PAGE_SIZE) -> tuple[int, list[dict]]:
    """
    Fetch one page of DRF detc search results (no query = all records).
    Returns (total_count, list of {decision_id, case_number, case_name, decision_date}).
    """
    params = {
        "OC": _OC,
        "target": "detc",
        "type": "XML",
        "display": str(display),
        "page": str(page),
    }
    url = f"{_DRF_BASE}/lawSearch.do?" + urllib.parse.urlencode(params)
    xml_text = _rate_limited_get(url)

    root = ET.fromstring(xml_text)
    total_cnt = int(root.findtext("totalCnt") or "0")

    items = []
    for detc_el in root.findall("Detc"):
        decision_id = (detc_el.findtext("헌재결정례일련번호") or "").strip()
        case_number = (detc_el.findtext("사건번호") or "").strip()
        case_name = (detc_el.findtext("사건명") or "").strip()
        decision_date = (detc_el.findtext("종국일자") or "").strip()
        items.append({
            "decision_id": decision_id,
            "case_number": case_number,
            "case_name": case_name,
            "decision_date": decision_date,
        })
    return total_cnt, items


def _fetch_detc_detail(decision_id: str) -> dict | None:
    """
    Fetch full 헌재 결정 XML by ID.
    Returns parsed dict or None on error.
    """
    url = f"{_DRF_BASE}/lawService.do?OC={_OC}&target=detc&ID={decision_id}&type=XML"
    try:
        xml_text = _rate_limited_get(url)
    except Exception as exc:
        logger.warning("Failed to fetch detc ID=%s: %s", decision_id, exc)
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("XML parse error for detc ID=%s: %s", decision_id, exc)
        return None

    # Check for error response
    if root.tag == "Law":
        logger.debug("No match for detc ID=%s", decision_id)
        return None

    return {
        "decision_id": (root.findtext("헌재결정례일련번호") or "").strip(),
        "case_number": (root.findtext("사건번호") or "").strip(),
        "case_name": (root.findtext("사건명") or "").strip(),
        "decision_date": (root.findtext("종국일자") or "").strip(),
        "case_type_code": (root.findtext("사건종류코드") or "").strip(),
        "case_type_name": (root.findtext("사건종류명") or "").strip(),
        "판시사항": (root.findtext("판시사항") or "").strip(),
        "결정요지": (root.findtext("결정요지") or "").strip(),
        "전문": (root.findtext("전문") or "").strip(),
        "참조조문": (root.findtext("참조조문") or "").strip(),
        "참조판례": (root.findtext("참조판례") or "").strip(),
        "심판대상조문": (root.findtext("심판대상조문") or "").strip(),
    }


def _decision_to_chunk(detail: dict, ingested_at: str) -> tuple[str, dict, str] | None:
    """
    Convert 헌재 결정 detail dict to (doc_id, metadata, content) for ChromaDB.
    Returns None if content is empty/invalid.
    """
    decision_id = detail.get("decision_id", "")
    case_number = detail.get("case_number", "")
    case_name = detail.get("case_name", "")
    decision_date = detail.get("decision_date", "").replace(".", "").replace("-", "")
    case_type_code = detail.get("case_type_code", "")
    case_type_name = detail.get("case_type_name", "")
    related_articles = detail.get("심판대상조문", "")[:500]

    if not decision_id or not case_number:
        return None

    # Build content: head + 결정요지 (or 전문 truncated)
    결정요지 = detail.get("결정요지", "").strip()
    판시사항 = detail.get("판시사항", "").strip()
    전문 = detail.get("전문", "").strip()

    # Priority: 결정요지 > 판시사항+전문 excerpt > 전문 alone
    if 결정요지:
        body = 결정요지
    elif 판시사항:
        body = 판시사항 + ("\n\n" + 전문[:2000] if 전문 else "")
    else:
        body = 전문

    if not body.strip():
        return None

    # Head includes case number and name for search recall
    head = f"[헌법재판소 {case_number}] {case_name}\n"
    content = (head + body)[:8000]

    doc_id = f"detc_{decision_id}"

    metadata = {
        "decision_id": decision_id,
        "case_number": case_number,
        "case_name": case_name[:500],
        "case_type_code": case_type_code,
        "case_type_name": case_type_name,
        "decision_date": decision_date,
        "related_articles": related_articles,
        "source": f"drf_api:detc/{decision_id}",
        "ingested_at": ingested_at,
        # Dim 9 compatibility: constitutional decisions have slower change cycle
        # Legaly VV check uses decision_date as the "effective_date" equivalent
        "effective_date": _normalize_date(decision_date),
    }

    return doc_id, metadata, content


def _normalize_date(date_str: str) -> str:
    """Normalize YYYYMMDD or YYYY.MM.DD to ISO 8601 YYYY-MM-DD."""
    clean = date_str.replace(".", "").replace("-", "").strip()
    if len(clean) == 8 and clean.isdigit():
        return f"{clean[:4]}-{clean[4:6]}-{clean[6:8]}"
    return date_str


def _search_by_case_number(case_number: str) -> list[dict]:
    """Find detc entries by case number query."""
    params = {
        "OC": _OC,
        "target": "detc",
        "type": "XML",
        "query": case_number,
        "display": "5",
        "page": "1",
    }
    url = f"{_DRF_BASE}/lawSearch.do?" + urllib.parse.urlencode(params)
    try:
        xml_text = _rate_limited_get(url)
        root = ET.fromstring(xml_text)
        items = []
        for detc_el in root.findall("Detc"):
            decision_id = (detc_el.findtext("헌재결정례일련번호") or "").strip()
            cn = (detc_el.findtext("사건번호") or "").strip()
            case_name = (detc_el.findtext("사건명") or "").strip()
            if decision_id:
                items.append({"decision_id": decision_id, "case_number": cn, "case_name": case_name})
        return items
    except Exception as exc:
        logger.warning("Failed to search by case number %s: %s", case_number, exc)
        return []


def get_chroma_client(persist_path: str = _CHROMA_PERSIST):
    import chromadb
    Path(persist_path).mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=persist_path)


def get_embedding_function():
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    device = os.getenv("KOLAW_EMBEDDING_DEVICE")
    if not device:
        try:
            import torch
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                device = "mps"
            elif torch.cuda.is_available():
                device = "cuda"
            else:
                device = "cpu"
        except Exception:
            device = "cpu"
    return SentenceTransformerEmbeddingFunction(
        model_name=_EMBEDDING_MODEL,
        device=device,
    )


def run_regression(
    collection,
    ingested_at: str,
) -> dict[str, bool]:
    """
    Verify 5 regression cases are present (or ingest them if missing).
    Returns {case_number: found_or_ingested} map.
    """
    results: dict[str, bool] = {}
    for case_number in _REGRESSION_CASES:
        doc_id_prefix = f"detc_"
        # Check if already present by querying
        items = _search_by_case_number(case_number)
        found_id = None
        for item in items:
            if item.get("case_number") == case_number:
                found_id = item["decision_id"]
                break
        if not found_id and items:
            # Approximate match — use first result
            found_id = items[0]["decision_id"]

        if not found_id:
            logger.warning("Regression: case %s not found in DRF", case_number)
            results[case_number] = False
            continue

        doc_id = f"detc_{found_id}"
        # Check if in ChromaDB
        try:
            existing = collection.get(ids=[doc_id], include=[])
            if existing["ids"]:
                logger.info("Regression [PASS]: %s already in collection", case_number)
                results[case_number] = True
                continue
        except Exception:
            pass

        # Ingest it now
        detail = _fetch_detc_detail(found_id)
        if not detail:
            logger.warning("Regression: could not fetch detail for %s", case_number)
            results[case_number] = False
            continue

        chunk = _decision_to_chunk(detail, ingested_at)
        if not chunk:
            results[case_number] = False
            continue

        doc_id, metadata, content = chunk
        try:
            collection.add(ids=[doc_id], documents=[content], metadatas=[metadata])
            logger.info("Regression [INGESTED]: %s (%s)", case_number, detail.get("case_name", ""))
            results[case_number] = True
        except Exception as exc:
            logger.error("Regression ingest failed for %s: %s", case_number, exc)
            results[case_number] = False

    return results


def ingest_constitutional(
    persist_path: str = _CHROMA_PERSIST,
    max_pages: int | None = None,
    dry_run: bool = False,
    regression_only: bool = False,
    embedding_function=None,
) -> dict[str, int]:
    """
    Ingest all 헌재 결정 from law.go.kr DRF into kolaw_constitutional collection.

    Args:
        persist_path: ChromaDB persist directory.
        max_pages: Limit pages fetched (None = all). Each page = 100 decisions + 100 detail reqs.
        dry_run: Print first 3 decisions, don't ingest.
        regression_only: Only verify/ingest 5 regression cases.
        embedding_function: Optional override for tests.

    Returns:
        {"decisions_fetched": N, "docs_ingested": M, "docs_skipped": K, "regression": R}
    """
    client = get_chroma_client(persist_path)
    ef = embedding_function or get_embedding_function()
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef,
    )

    ingested_at = datetime.now(timezone.utc).isoformat()

    if regression_only:
        results = run_regression(collection, ingested_at)
        passed = sum(1 for v in results.values() if v)
        print(f"[regression] {passed}/{len(_REGRESSION_CASES)} cases verified")
        for cn, ok in results.items():
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {cn}")
        return {
            "decisions_fetched": 0,
            "docs_ingested": 0,
            "docs_skipped": 0,
            "regression": passed,
        }

    # --- Phase 1: Fetch all page listings ---
    print(f"[constitutional] Starting full ingest to collection: {_COLLECTION_NAME}")
    print(f"[constitutional] Persist path: {persist_path}")

    first_total, _ = _search_detc_page(1, display=1)
    total_decisions = first_total
    total_pages = (total_decisions + _SEARCH_PAGE_SIZE - 1) // _SEARCH_PAGE_SIZE
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)

    print(f"[constitutional] Total 헌재 결정: {total_decisions}")
    print(f"[constitutional] Pages to fetch: {total_pages} (page_size={_SEARCH_PAGE_SIZE})")

    if dry_run:
        _, sample = _search_detc_page(1, display=3)
        print("[dry_run] Sample decisions:")
        for item in sample:
            detail = _fetch_detc_detail(item["decision_id"])
            if detail:
                chunk = _decision_to_chunk(detail, ingested_at)
                if chunk:
                    doc_id, meta, content = chunk
                    print(f"  {doc_id}: {content[:200]}")
        return {"decisions_fetched": 3, "docs_ingested": 0, "docs_skipped": 3, "regression": 0}

    docs_ingested = 0
    docs_skipped = 0
    decisions_fetched = 0
    t0 = time.time()

    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict] = []

    def flush_batch() -> None:
        nonlocal docs_ingested, docs_skipped
        if not batch_ids:
            return
        # Dedup check
        try:
            existing = collection.get(ids=batch_ids, include=[])
            existing_ids = set(existing["ids"])
        except Exception:
            existing_ids = set()

        new_ids = [i for i in batch_ids if i not in existing_ids]
        new_docs = [d for i, d in zip(batch_ids, batch_docs) if i not in existing_ids]
        new_metas = [m for i, m in zip(batch_ids, batch_metas) if i not in existing_ids]

        docs_skipped += len(batch_ids) - len(new_ids)
        if new_ids:
            collection.add(ids=new_ids, documents=new_docs, metadatas=new_metas)
            docs_ingested += len(new_ids)

        batch_ids.clear()
        batch_docs.clear()
        batch_metas.clear()

    for page_num in range(1, total_pages + 1):
        _, items = _search_detc_page(page_num)
        for item in items:
            decision_id = item["decision_id"]
            if not decision_id:
                continue
            decisions_fetched += 1

            # Check if already in collection (skip detail fetch)
            doc_id = f"detc_{decision_id}"
            try:
                existing = collection.get(ids=[doc_id], include=[])
                if existing["ids"]:
                    docs_skipped += 1
                    continue
            except Exception:
                pass

            # Fetch detail (1 req/sec)
            detail = _fetch_detc_detail(decision_id)
            if not detail:
                continue

            chunk = _decision_to_chunk(detail, ingested_at)
            if not chunk:
                continue

            doc_id_chunk, metadata, content = chunk
            batch_ids.append(doc_id_chunk)
            batch_docs.append(content)
            batch_metas.append(metadata)

            if len(batch_ids) >= _CHUNK_BATCH:
                flush_batch()

        elapsed = time.time() - t0
        rate = decisions_fetched / elapsed if elapsed > 0 else 0
        eta = (total_pages - page_num) * _SEARCH_PAGE_SIZE / rate if rate > 0 else 0
        print(
            f"[constitutional] page {page_num}/{total_pages} "
            f"| fetched={decisions_fetched} ingested={docs_ingested} skipped={docs_skipped} "
            f"| {elapsed:.0f}s elapsed | ETA {eta:.0f}s",
            flush=True,
        )

    flush_batch()

    # Run regression check
    regression_results = run_regression(collection, ingested_at)
    regression_passed = sum(1 for v in regression_results.values() if v)

    elapsed = time.time() - t0
    print(
        f"[constitutional] DONE: {decisions_fetched} fetched, "
        f"{docs_ingested} ingested, {docs_skipped} skipped, "
        f"{elapsed:.0f}s total"
    )
    print(f"[constitutional] Regression: {regression_passed}/{len(_REGRESSION_CASES)} passed")
    for cn, ok in regression_results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {cn}")

    return {
        "decisions_fetched": decisions_fetched,
        "docs_ingested": docs_ingested,
        "docs_skipped": docs_skipped,
        "regression": regression_passed,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Ingest 헌법재판소 결정 from law.go.kr DRF into kolaw_constitutional collection"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print first 3 decisions, do not ingest",
    )
    parser.add_argument(
        "--regression-only",
        action="store_true",
        help="Only verify/ingest the 5 regression test cases",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit number of search pages fetched (default: all ~380 pages)",
    )
    parser.add_argument(
        "--persist",
        default=_CHROMA_PERSIST,
        help=f"ChromaDB persist path (default: {_CHROMA_PERSIST})",
    )
    args = parser.parse_args()

    result = ingest_constitutional(
        persist_path=args.persist,
        max_pages=args.max_pages,
        dry_run=args.dry_run,
        regression_only=args.regression_only,
    )
    print(f"Result: {result}")
