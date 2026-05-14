"""
DRF API ingest for 판례 (court precedents) — Phase B.4.1.

Fetches precedents from law.go.kr DRF using target=prec and creates a new
ChromaDB collection kolaw_precedents (separate from kolaw_laws* collections).

Sources: law.go.kr DRF API
  Search:  GET /DRF/lawSearch.do?OC=Hydrogen&target=prec&type=XML&query=<law>
  Detail:  GET /DRF/lawService.do?OC=Hydrogen&target=prec&ID=<id>&type=XML

Rate limit: 1 req/sec strict (law.go.kr TOS, robots.txt compliance).

P1 target laws (initial ingest): 민법, 의료법, 근로기준법, 형법, 자본시장법
  Each law — paginate through all available results (max 100/page).
  Detail fetch is skipped if ID already in collection (dedup).

Collection schema:
  doc_id:   prec_<판례일련번호>
  document: "[<법원명> <사건번호> (<선고일자>)] <사건명>\\n\\n판시사항:\\n<text>\\n\\n판결요지:\\n<text>"
  metadata:
    prec_id:       판례일련번호 (str)
    case_name:     사건명
    case_number:   사건번호
    court_name:    법원명
    court_type:    법원종류코드
    decision_date: 선고일자 (YYYYMMDD)
    case_type:     사건종류명
    case_type_code: 사건종류코드
    decision_type: 판결유형
    related_law:   쿼리 법령명 (검색 키워드)
    source:        데이터출처명
    detail_url:    판례상세링크
    ingested_at:   ISO8601 UTC

Usage:
  python -m services.fast_search.ingest_precedents
  python -m services.fast_search.ingest_precedents --laws 민법 형법 --dry-run
  python -m services.fast_search.ingest_precedents --laws 의료법 --max-per-law 50
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

logger = logging.getLogger(__name__)

_OC = os.getenv("LAW_GO_KR_OC", "Hydrogen")
_DRF_BASE = "https://www.law.go.kr/DRF"
_RATE_LIMIT_SEC = 1.0
_LAST_REQUEST_TIME = [0.0]

_CHROMA_PERSIST = os.getenv(
    "CHROMA_PERSIST_PATH",
    str(Path(__file__).parent / "chroma_db"),
)
_COLLECTION_NAME = os.getenv("KOLAW_PREC_COLLECTION", "kolaw_precedents")
_EMBEDDING_MODEL = os.getenv("KOLAW_EMBEDDING_MODEL", "jhgan/ko-sroberta-multitask")

# P1 priority laws for initial ingest
P1_LAWS = ["민법", "의료법", "근로기준법", "형법", "자본시장법"]

# Max results per law per page (API caps at 100)
_PAGE_SIZE = 100


def _rate_limited_get(url: str, timeout: int = 30) -> str:
    """Fetch URL enforcing 1 req/sec rate limit."""
    elapsed = time.time() - _LAST_REQUEST_TIME[0]
    if elapsed < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - elapsed)
    _LAST_REQUEST_TIME[0] = time.time()
    req = urllib.request.urlopen(url, timeout=timeout)
    return req.read().decode("utf-8")


def _search_precedents(query: str, page: int = 1, display: int = _PAGE_SIZE) -> tuple[int, list[dict]]:
    """
    Search precedents by keyword.
    Returns (total_count, list of prec dicts).
    """
    params = {
        "OC": _OC,
        "target": "prec",
        "type": "XML",
        "query": query,
        "display": str(display),
        "page": str(page),
    }
    url = f"{_DRF_BASE}/lawSearch.do?" + urllib.parse.urlencode(params)
    logger.debug("Searching precedents: %s (page=%d)", query, page)
    xml_text = _rate_limited_get(url)

    root = ET.fromstring(xml_text)
    total = int(root.findtext("totalCnt") or "0")
    results = []
    for prec_el in root.findall("prec"):
        results.append({
            "prec_id": (prec_el.findtext("판례일련번호") or "").strip(),
            "case_name": (prec_el.findtext("사건명") or "").strip(),
            "case_number": (prec_el.findtext("사건번호") or "").strip(),
            "decision_date": (prec_el.findtext("선고일자") or "").strip(),
            "court_name": (prec_el.findtext("법원명") or "").strip(),
            "court_type": (prec_el.findtext("법원종류코드") or "").strip(),
            "case_type": (prec_el.findtext("사건종류명") or "").strip(),
            "case_type_code": (prec_el.findtext("사건종류코드") or "").strip(),
            "decision_type": (prec_el.findtext("판결유형") or "").strip(),
            "선고": (prec_el.findtext("선고") or "").strip(),
            "source": (prec_el.findtext("데이터출처명") or "").strip(),
            "detail_url": (prec_el.findtext("판례상세링크") or "").strip(),
        })
    return total, results


def _fetch_precedent_detail(prec_id: str) -> dict:
    """
    Fetch full precedent detail (판시사항 + 판결요지 + 이유) via XML.
    Returns dict with keys: 판시사항, 판결요지, 이유 (may be empty strings).
    """
    url = f"{_DRF_BASE}/lawService.do?OC={_OC}&target=prec&ID={prec_id}&type=XML"
    logger.debug("Fetching precedent detail ID=%s", prec_id)
    xml_text = _rate_limited_get(url)

    root = ET.fromstring(xml_text)

    def _clean(text: str | None) -> str:
        if not text:
            return ""
        import re
        # Remove HTML tags from CDATA content
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return text.strip()

    # Update base metadata fields from detail (more reliable than search)
    result = {
        "court_name": (root.findtext("법원명") or "").strip(),
        "court_type": (root.findtext("법원종류코드") or "").strip(),
        "case_type": (root.findtext("사건종류명") or "").strip(),
        "case_type_code": (root.findtext("사건종류코드") or "").strip(),
        "decision_type": (root.findtext("판결유형") or "").strip(),
        "decision_date": (root.findtext("선고일자") or "").strip(),
        "판시사항": _clean(root.findtext("판시사항")),
        "판결요지": _clean(root.findtext("판결요지")),
        "이유": _clean(root.findtext("참조조문") or ""),  # sometimes useful
    }
    return result


def _build_document(search_item: dict, detail: dict) -> str:
    """
    Build ChromaDB document text from search result + detail data.
    Format: header + 판시사항 + 판결요지
    """
    court = detail.get("court_name") or search_item.get("court_name") or ""
    case_num = search_item["case_number"]
    decision_date = detail.get("decision_date") or search_item.get("decision_date") or ""
    case_name = search_item["case_name"]
    판시사항 = detail.get("판시사항", "")
    판결요지 = detail.get("판결요지", "")

    # Format date as YYYY.MM.DD for display
    if len(decision_date) == 8:
        display_date = f"{decision_date[:4]}.{decision_date[4:6]}.{decision_date[6:]}"
    else:
        display_date = decision_date

    header = f"[{court} {case_num} ({display_date})] {case_name}"
    parts = [header]
    if 판시사항:
        parts.append(f"\n판시사항:\n{판시사항}")
    if 판결요지:
        parts.append(f"\n판결요지:\n{판결요지}")

    return "\n".join(parts)[:8000]


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


def ingest_precedents(
    laws: list[str] | None = None,
    max_per_law: int | None = None,
    collection_name: str = _COLLECTION_NAME,
    persist_path: str = _CHROMA_PERSIST,
    embedding_function=None,
    dry_run: bool = False,
) -> dict:
    """
    Ingest court precedents into kolaw_precedents ChromaDB collection.

    Args:
        laws: List of law names to query. Defaults to P1_LAWS.
        max_per_law: Maximum precedents to ingest per law (None = all available).
        collection_name: ChromaDB collection name.
        persist_path: ChromaDB persist directory.
        embedding_function: Optional embedding function override (for tests).
        dry_run: If True, print first 3 items per law without ingesting.

    Returns:
        dict with keys: laws_processed, docs_ingested, docs_skipped, errors
    """
    if laws is None:
        laws = P1_LAWS

    client = None
    collection = None
    ef = None

    if not dry_run:
        client = get_chroma_client(persist_path)
        ef = embedding_function or get_embedding_function()
        collection = client.get_or_create_collection(
            name=collection_name,
            embedding_function=ef,
        )

    ingested_at = datetime.now(timezone.utc).isoformat()
    total_ingested = 0
    total_skipped = 0
    total_errors = 0
    laws_processed = 0

    for law_name in laws:
        logger.info("Processing law: %s", law_name)
        law_ingested = 0
        law_skipped = 0
        page = 1

        # Collect all search results first (paginate)
        all_search_results: list[dict] = []
        while True:
            total_count, results = _search_precedents(law_name, page=page)
            if not results:
                break
            all_search_results.extend(results)
            logger.info("  Page %d: got %d results (total=%d so far=%d)",
                        page, len(results), total_count, len(all_search_results))

            if len(all_search_results) >= total_count:
                break
            if max_per_law and len(all_search_results) >= max_per_law:
                break
            if len(results) < _PAGE_SIZE:
                break
            page += 1

        if max_per_law:
            all_search_results = all_search_results[:max_per_law]

        logger.info("  Total to process for %s: %d", law_name, len(all_search_results))

        # Dedup check: which IDs are already in collection
        if collection is not None:
            candidate_ids = [f"prec_{r['prec_id']}" for r in all_search_results if r["prec_id"]]
            try:
                existing = collection.get(ids=candidate_ids, include=[])
                existing_ids = set(existing["ids"])
            except Exception:
                existing_ids = set()
        else:
            existing_ids = set()

        for i, item in enumerate(all_search_results):
            prec_id = item["prec_id"]
            if not prec_id:
                continue

            doc_id = f"prec_{prec_id}"

            if doc_id in existing_ids:
                law_skipped += 1
                continue

            if dry_run:
                if law_ingested < 3:
                    print(f"  [dry_run] {doc_id}: {item['case_name'][:80]}")
                law_ingested += 1
                continue

            # Fetch detail (rate-limited)
            try:
                detail = _fetch_precedent_detail(prec_id)
            except Exception as exc:
                logger.warning("  Detail fetch failed for %s: %s", prec_id, exc)
                detail = {}
                total_errors += 1

            doc_text = _build_document(item, detail)

            # Build metadata — all values must be str (ChromaDB requirement)
            court_name = (detail.get("court_name") or item.get("court_name") or "").strip()
            decision_date = (detail.get("decision_date") or item.get("decision_date") or "").replace(".", "")
            # Normalize date to YYYYMMDD
            if len(decision_date) == 8:
                pass  # already YYYYMMDD
            elif len(decision_date) == 7:
                decision_date = ""  # malformed

            metadata = {
                "prec_id": str(prec_id),
                "case_name": item["case_name"][:500],
                "case_number": item["case_number"],
                "court_name": court_name,
                "court_type": (detail.get("court_type") or item.get("court_type") or "").strip(),
                "decision_date": decision_date,
                "case_type": (detail.get("case_type") or item.get("case_type") or "").strip(),
                "case_type_code": (detail.get("case_type_code") or item.get("case_type_code") or "").strip(),
                "decision_type": (detail.get("decision_type") or item.get("decision_type") or "").strip(),
                "related_law": law_name,
                "source": item.get("source", ""),
                "detail_url": item.get("detail_url", ""),
                "ingested_at": ingested_at,
            }

            collection.add(
                ids=[doc_id],
                documents=[doc_text],
                metadatas=[metadata],
            )
            law_ingested += 1

            if law_ingested % 20 == 0:
                print(
                    f"[ingest_precedents] {law_name}: {law_ingested} ingested, "
                    f"{law_skipped} skipped so far",
                    flush=True,
                )

        total_ingested += law_ingested
        total_skipped += law_skipped
        laws_processed += 1
        print(
            f"[ingest_precedents] {law_name} done: "
            f"ingested={law_ingested} skipped={law_skipped}",
            flush=True,
        )

    print(
        f"[ingest_precedents] DONE: {laws_processed} laws, "
        f"{total_ingested} ingested, {total_skipped} skipped, "
        f"{total_errors} errors",
        flush=True,
    )
    return {
        "laws_processed": laws_processed,
        "docs_ingested": total_ingested,
        "docs_skipped": total_skipped,
        "errors": total_errors,
        "collection": collection_name,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Ingest 판례 from law.go.kr DRF into kolaw_precedents")
    parser.add_argument(
        "--laws",
        nargs="+",
        default=None,
        metavar="LAW",
        help=f"Law names to query (default: P1 = {', '.join(P1_LAWS)})",
    )
    parser.add_argument(
        "--max-per-law",
        type=int,
        default=None,
        help="Max precedents per law (default: all)",
    )
    parser.add_argument(
        "--collection",
        default=_COLLECTION_NAME,
        help=f"ChromaDB collection name (default: {_COLLECTION_NAME})",
    )
    parser.add_argument(
        "--persist",
        default=_CHROMA_PERSIST,
        help=f"ChromaDB persist path (default: {_CHROMA_PERSIST})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print first 3 items per law without ingesting",
    )
    args = parser.parse_args()

    result = ingest_precedents(
        laws=args.laws,
        max_per_law=args.max_per_law,
        collection_name=args.collection,
        persist_path=args.persist,
        dry_run=args.dry_run,
    )
    print(f"Result: {result}")
