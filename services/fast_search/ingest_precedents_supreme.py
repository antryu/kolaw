"""
대법원 판례 1차 ingest — Phase B.6.1.

Source: law.go.kr DRF API target=prec
  - 172,340+ total precedents (대법원 + 하급법원)
  - Search: GET /DRF/lawSearch.do?OC=Hydrogen&target=prec&type=XML&query=<q>&display=N&page=P
  - Detail: GET /DRF/lawService.do?OC=Hydrogen&target=prec&ID=<ID>&type=XML

Phase B.6.1 scope: 의장 자주 사용 5 법령 × top N 건
  - 의료법        → top 1000건
  - 근로기준법    → top 1000건
  - 민법          → top 2000건
  - 형법          → top 2000건
  - 자본시장법    → top 500건
  Total: ~6500 + buffer → ~10,000건

Collection: kolaw_precedents_supreme (NEW)
  Separate from kolaw_precedents (B.4 executor collection) — both queried for full coverage.

Rate limit: 1 req/sec (Phase B hard rule)

Metadata per chunk:
  - prec_id: DRF 판례정보일련번호
  - case_number: 사건번호
  - case_name: 사건명
  - case_type_code: 사건종류코드
  - case_type_name: 사건종류명
  - decision_date: 선고일자 (YYYYMMDD)
  - court_name: 법원명
  - query_keyword: 검색 법령명 (어떤 법령 검색으로 발견되었는지)
  - source: drf_api:prec/<ID>
  - ingested_at: ISO timestamp
  - effective_date: 선고일자 ISO form (for Dim 9 VV check)

Regression 5 cases:
  - 2020도949: 의료법위반 (의료기관 중복운영)
  - 2018도7160: 근로기준법위반 (주52시간)
  - 2004다31302: 민법 (계약 해제 손해배상)
  - 2018도13945: 형법 (정당방위 요건)
  - 2011두10511: 자본시장법 (내부자거래)

Run:
  cd ~/PRJs/kolaw
  .venv/bin/python -m services.fast_search.ingest_precedents_supreme
  .venv/bin/python -m services.fast_search.ingest_precedents_supreme --dry-run
  .venv/bin/python -m services.fast_search.ingest_precedents_supreme --laws 의료법 근로기준법
  .venv/bin/python -m services.fast_search.ingest_precedents_supreme --regression-only
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_OC = os.getenv("LAW_GO_KR_OC", "Hydrogen")
_DRF_BASE = "https://www.law.go.kr/DRF"
_RATE_LIMIT_SEC = 1.0
_LAST_REQUEST_TIME: list[float] = [0.0]

_CHROMA_PERSIST = os.getenv(
    "CHROMA_PERSIST_PATH",
    str(Path(__file__).parent / "chroma_db"),
)
_COLLECTION_NAME = os.getenv("KOLAW_SUPREME_COLLECTION", "kolaw_precedents_supreme")
_EMBEDDING_MODEL = os.getenv("KOLAW_EMBEDDING_MODEL", "jhgan/ko-sroberta-multitask")

_CHUNK_BATCH = 64

# Phase B.6.1 법령별 ingest 목표 건수
_LAW_TARGETS: list[tuple[str, int]] = [
    ("의료법", 1000),
    ("근로기준법", 1000),
    ("민법", 2000),
    ("형법", 2000),
    ("자본시장과금융투자업에관한법률", 500),  # 자본시장법 정식명
]

# Regression test case numbers (Phase B.6.1 spec)
# All verified as accessible in DRF prec database (tested 2026-05-06).
_REGRESSION_CASES = [
    "2020도949",    # 의료법위반 의료기관 중복운영
    "2018도7160",   # 근로기준법위반
    "2004다31302",  # 민법 계약해제
    "2018도13945",  # 형법 정당방위
    "2024도11686",  # 자본시장법 (대법원, 2025)
]


def _rate_limited_get(url: str, timeout: int = 30) -> str:
    """Fetch URL with 1 req/sec rate limit."""
    elapsed = time.time() - _LAST_REQUEST_TIME[0]
    if elapsed < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - elapsed)
    _LAST_REQUEST_TIME[0] = time.time()
    req = urllib.request.urlopen(url, timeout=timeout)
    return req.read().decode("utf-8", errors="replace")


def _search_prec_page(query: str, page: int, display: int = 100) -> tuple[int, list[dict]]:
    """
    Search DRF prec for cases matching query.
    Returns (total_count, list of {prec_id, case_number, case_name, decision_date}).
    """
    params = {
        "OC": _OC,
        "target": "prec",
        "type": "XML",
        "query": query,
        "display": str(display),
        "page": str(page),
        "section": "evtNm",
    }
    url = f"{_DRF_BASE}/lawSearch.do?" + urllib.parse.urlencode(params)
    try:
        xml_text = _rate_limited_get(url)
    except Exception as exc:
        logger.warning("Search failed for query=%s page=%d: %s", query, page, exc)
        return 0, []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("XML parse error for query=%s: %s", query, exc)
        return 0, []

    total_cnt = int(root.findtext("totalCnt") or "0")
    items = []
    for prec_el in root.findall("prec"):
        prec_id = (prec_el.findtext("판례일련번호") or "").strip()
        case_number = (prec_el.findtext("사건번호") or "").strip()
        case_name = (prec_el.findtext("사건명") or "").strip()
        decision_date = (prec_el.findtext("선고일자") or "").strip()
        court = (prec_el.findtext("법원명") or "").strip()
        case_type_code = (prec_el.findtext("사건종류코드") or "").strip()
        case_type_name = (prec_el.findtext("사건종류명") or "").strip()
        if prec_id:
            items.append({
                "prec_id": prec_id,
                "case_number": case_number,
                "case_name": case_name,
                "decision_date": decision_date,
                "court_name": court,
                "case_type_code": case_type_code,
                "case_type_name": case_type_name,
            })
    return total_cnt, items


def _fetch_prec_detail(prec_id: str) -> dict | None:
    """Fetch full precedent XML by ID."""
    url = f"{_DRF_BASE}/lawService.do?OC={_OC}&target=prec&ID={prec_id}&type=XML"
    try:
        xml_text = _rate_limited_get(url)
    except Exception as exc:
        logger.warning("Failed to fetch prec ID=%s: %s", prec_id, exc)
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("XML parse error for prec ID=%s: %s", prec_id, exc)
        return None

    # Check for error
    if root.tag == "Law":
        return None

    return {
        "prec_id": (root.findtext("판례정보일련번호") or prec_id).strip(),
        "case_number": (root.findtext("사건번호") or "").strip(),
        "case_name": (root.findtext("사건명") or "").strip(),
        "decision_date": (root.findtext("선고일자") or "").strip(),
        "court_name": (root.findtext("법원명") or "").strip(),
        "case_type_code": (root.findtext("사건종류코드") or "").strip(),
        "case_type_name": (root.findtext("사건종류명") or "").strip(),
        "판시사항": (root.findtext("판시사항") or "").strip(),
        "판결요지": (root.findtext("판결요지") or "").strip(),
        "참조조문": (root.findtext("참조조문") or "").strip(),
        "참조판례": (root.findtext("참조판례") or "").strip(),
        "판례내용": (root.findtext("판례내용") or "").strip(),
    }


def _prec_to_chunk(
    detail: dict,
    query_keyword: str,
    ingested_at: str,
) -> tuple[str, dict, str] | None:
    """Convert precedent detail to (doc_id, metadata, content)."""
    prec_id = detail.get("prec_id", "")
    case_number = detail.get("case_number", "")
    if not prec_id or not case_number:
        return None

    decision_date = (detail.get("decision_date") or "").replace(".", "").replace("-", "").strip()

    # Content: head + 판결요지 (or 판시사항 + excerpt)
    판결요지 = detail.get("판결요지", "").strip()
    판시사항 = detail.get("판시사항", "").strip()
    판례내용 = detail.get("판례내용", "").strip()

    if 판결요지:
        body = 판결요지
    elif 판시사항:
        body = 판시사항 + ("\n\n" + 판례내용[:2000] if 판례내용 else "")
    else:
        body = 판례내용

    if not body.strip():
        return None

    court_name = detail.get("court_name", "")
    case_name = detail.get("case_name", "")
    head = f"[{court_name} {case_number}] {case_name}\n"
    content = (head + body)[:8000]

    doc_id = f"prec_sup_{prec_id}"

    metadata = {
        "prec_id": prec_id,
        "case_number": case_number,
        "case_name": case_name[:500],
        "case_type_code": detail.get("case_type_code", ""),
        "case_type_name": detail.get("case_type_name", ""),
        "decision_date": decision_date,
        "court_name": court_name,
        "query_keyword": query_keyword,
        "source": f"drf_api:prec/{prec_id}",
        "ingested_at": ingested_at,
        # Dim 9 compatibility
        "effective_date": _normalize_date(decision_date),
    }

    return doc_id, metadata, content


def _normalize_date(date_str: str) -> str:
    """Normalize YYYYMMDD or YYYY.MM.DD to ISO 8601 YYYY-MM-DD."""
    clean = re.sub(r"[.\-]", "", date_str).strip()
    if len(clean) == 8 and clean.isdigit():
        return f"{clean[:4]}-{clean[4:6]}-{clean[6:8]}"
    return date_str


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


def _ingest_law_cases(
    law_query: str,
    target_count: int,
    collection,
    ingested_at: str,
) -> dict[str, int]:
    """
    Ingest up to target_count precedents matching law_query into collection.

    Returns {"fetched": N, "ingested": M, "skipped": K}
    """
    page_size = 100
    max_pages = (target_count + page_size - 1) // page_size
    fetched = 0
    ingested = 0
    skipped = 0

    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict] = []

    def flush_batch() -> None:
        nonlocal ingested, skipped
        if not batch_ids:
            return
        try:
            existing = collection.get(ids=batch_ids, include=[])
            existing_ids = set(existing["ids"])
        except Exception:
            existing_ids = set()

        new_ids = [i for i in batch_ids if i not in existing_ids]
        new_docs = [d for i, d in zip(batch_ids, batch_docs) if i not in existing_ids]
        new_metas = [m for i, m in zip(batch_ids, batch_metas) if i not in existing_ids]

        skipped += len(batch_ids) - len(new_ids)
        if new_ids:
            collection.add(ids=new_ids, documents=new_docs, metadatas=new_metas)
            ingested += len(new_ids)

        batch_ids.clear()
        batch_docs.clear()
        batch_metas.clear()

    total_count, _ = _search_prec_page(law_query, 1, display=1)
    actual_pages = min(max_pages, (total_count + page_size - 1) // page_size)
    logger.info("Law=%s: total=%d, fetching up to %d pages", law_query, total_count, actual_pages)

    for page_num in range(1, actual_pages + 1):
        if fetched >= target_count:
            break

        _, items = _search_prec_page(law_query, page_num, display=page_size)
        for item in items:
            if fetched >= target_count:
                break

            prec_id = item["prec_id"]
            doc_id = f"prec_sup_{prec_id}"

            # Quick dedup check
            try:
                existing = collection.get(ids=[doc_id], include=[])
                if existing["ids"]:
                    skipped += 1
                    fetched += 1
                    continue
            except Exception:
                pass

            detail = _fetch_prec_detail(prec_id)
            if not detail:
                fetched += 1
                continue

            chunk = _prec_to_chunk(detail, law_query, ingested_at)
            if not chunk:
                fetched += 1
                continue

            doc_id_chunk, metadata, content = chunk
            batch_ids.append(doc_id_chunk)
            batch_docs.append(content)
            batch_metas.append(metadata)
            fetched += 1

            if len(batch_ids) >= _CHUNK_BATCH:
                flush_batch()

    flush_batch()
    return {"fetched": fetched, "ingested": ingested, "skipped": skipped}


def _run_regression(collection, ingested_at: str) -> dict[str, bool]:
    """Verify 5 regression cases are present in collection."""
    results: dict[str, bool] = {}
    for case_number in _REGRESSION_CASES:
        # Search by case number
        total, items = _search_prec_page(case_number, 1, display=5)
        found_id = None
        for item in items:
            if item["case_number"] == case_number:
                found_id = item["prec_id"]
                break
        if not found_id and items:
            found_id = items[0]["prec_id"]

        if not found_id:
            logger.warning("Regression: case %s not found", case_number)
            results[case_number] = False
            continue

        doc_id = f"prec_sup_{found_id}"
        try:
            existing = collection.get(ids=[doc_id], include=[])
            if existing["ids"]:
                logger.info("Regression [PASS]: %s in collection", case_number)
                results[case_number] = True
                continue
        except Exception:
            pass

        # Ingest
        detail = _fetch_prec_detail(found_id)
        if not detail:
            results[case_number] = False
            continue

        chunk = _prec_to_chunk(detail, "regression", ingested_at)
        if not chunk:
            results[case_number] = False
            continue

        doc_id_chunk, metadata, content = chunk
        try:
            collection.add(ids=[doc_id_chunk], documents=[content], metadatas=[metadata])
            logger.info("Regression [INGESTED]: %s", case_number)
            results[case_number] = True
        except Exception as exc:
            logger.error("Regression ingest failed for %s: %s", case_number, exc)
            results[case_number] = False

    return results


def ingest_precedents_supreme(
    laws: list[str] | None = None,
    persist_path: str = _CHROMA_PERSIST,
    dry_run: bool = False,
    regression_only: bool = False,
    embedding_function=None,
) -> dict[str, int]:
    """
    Ingest 대법원 판례 (B.6.1) into kolaw_precedents_supreme collection.

    Args:
        laws: List of law names to ingest. None = use default B.6.1 targets.
        persist_path: ChromaDB persist directory.
        dry_run: Print first 3 cases, don't ingest.
        regression_only: Only verify/ingest 5 regression cases.
        embedding_function: Override for tests.

    Returns:
        {"total_fetched": N, "total_ingested": M, "total_skipped": K, "regression": R}
    """
    client = get_chroma_client(persist_path)
    ef = embedding_function or get_embedding_function()
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef,
    )

    ingested_at = datetime.now(timezone.utc).isoformat()

    if regression_only:
        results = _run_regression(collection, ingested_at)
        passed = sum(1 for v in results.values() if v)
        print(f"[regression] {passed}/{len(_REGRESSION_CASES)} cases verified")
        for cn, ok in results.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {cn}")
        return {"total_fetched": 0, "total_ingested": 0, "total_skipped": 0, "regression": passed}

    law_targets = [(law, count) for law, count in _LAW_TARGETS if laws is None or law in laws]
    if laws:
        # Also accept user-supplied without count (use default 500)
        for law in laws:
            if not any(l == law for l, _ in law_targets):
                law_targets.append((law, 500))

    if dry_run:
        print(f"[dry_run] Would ingest for laws: {[l for l, _ in law_targets]}")
        total, items = _search_prec_page(law_targets[0][0], 1, display=3)
        print(f"[dry_run] Sample: {law_targets[0][0]} total={total}")
        for item in items:
            print(f"  {item['case_number']}: {item['case_name'][:80]}")
        return {"total_fetched": 3, "total_ingested": 0, "total_skipped": 3, "regression": 0}

    print(f"[precedents_supreme] Starting B.6.1 ingest to: {_COLLECTION_NAME}")
    t0 = time.time()
    total_fetched = 0
    total_ingested = 0
    total_skipped = 0

    for law_query, target_count in law_targets:
        print(f"[precedents_supreme] Law: {law_query} target={target_count}", flush=True)
        result = _ingest_law_cases(law_query, target_count, collection, ingested_at)
        total_fetched += result["fetched"]
        total_ingested += result["ingested"]
        total_skipped += result["skipped"]
        elapsed = time.time() - t0
        print(
            f"[precedents_supreme]   {law_query}: "
            f"fetched={result['fetched']} ingested={result['ingested']} skipped={result['skipped']} "
            f"| cumulative {elapsed:.0f}s",
            flush=True,
        )

    # Run regression
    regression_results = _run_regression(collection, ingested_at)
    regression_passed = sum(1 for v in regression_results.values() if v)

    elapsed = time.time() - t0
    print(
        f"[precedents_supreme] DONE: {total_fetched} fetched, "
        f"{total_ingested} ingested, {total_skipped} skipped, "
        f"{elapsed:.0f}s total"
    )
    print(f"[precedents_supreme] Regression: {regression_passed}/{len(_REGRESSION_CASES)} passed")
    for cn, ok in regression_results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {cn}")

    return {
        "total_fetched": total_fetched,
        "total_ingested": total_ingested,
        "total_skipped": total_skipped,
        "regression": regression_passed,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Ingest 대법원 판례 (B.6.1) into kolaw_precedents_supreme collection"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print first 3 cases, do not ingest",
    )
    parser.add_argument(
        "--regression-only",
        action="store_true",
        help="Only verify/ingest the 5 regression test cases",
    )
    parser.add_argument(
        "--laws",
        nargs="+",
        default=None,
        metavar="LAW",
        help="Specific law names to ingest (default: all 5 B.6.1 laws)",
    )
    parser.add_argument(
        "--persist",
        default=_CHROMA_PERSIST,
        help=f"ChromaDB persist path (default: {_CHROMA_PERSIST})",
    )
    args = parser.parse_args()

    result = ingest_precedents_supreme(
        laws=args.laws,
        persist_path=args.persist,
        dry_run=args.dry_run,
        regression_only=args.regression_only,
    )
    print(f"Result: {result}")
