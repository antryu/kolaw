"""
DRF API ingest for 법령해석례 (legal interpretations) — Phase B.4.2.

Fetches 법제처 법령해석례 from law.go.kr DRF using target=expc and creates a
new ChromaDB collection kolaw_interpretations (separate from all kolaw_laws*
and kolaw_precedents collections).

Sources: law.go.kr DRF API
  Search:  GET /DRF/lawSearch.do?OC=Hydrogen&target=expc&type=XML&query=<law>
  Detail:  GET /DRF/lawService.do?OC=Hydrogen&target=expc&ID=<id>&type=XML

Rate limit: 1 req/sec strict (law.go.kr TOS, robots.txt compliance).

P1 target laws: 민법, 의료법, 근로기준법, 형법, 자본시장법

Collection schema:
  doc_id:   expc_<법령해석례일련번호>
  document: "[<회신기관명> <안건번호> (<해석일자>)] <안건명>\\n\\n질의요지:\\n<text>\\n\\n회답:\\n<text>\\n\\n이유:\\n<text>"
  metadata:
    expc_id:       법령해석례일련번호 (str)
    case_title:    안건명
    case_number:   안건번호
    decision_date: 해석일자 (YYYYMMDD)
    issuer:        회신기관명
    issuer_code:   회신기관코드
    requester:     질의기관명
    requester_code: 질의기관코드
    related_law:   쿼리 법령명
    detail_url:    법령해석례상세링크
    ingested_at:   ISO8601 UTC

Usage:
  python -m services.fast_search.ingest_interpretations
  python -m services.fast_search.ingest_interpretations --laws 근로기준법 --dry-run
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
_LAST_REQUEST_TIME = [0.0]

_CHROMA_PERSIST = os.getenv(
    "CHROMA_PERSIST_PATH",
    str(Path(__file__).parent / "chroma_db"),
)
_COLLECTION_NAME = os.getenv("KOLAW_EXPC_COLLECTION", "kolaw_interpretations")
_EMBEDDING_MODEL = os.getenv("KOLAW_EMBEDDING_MODEL", "jhgan/ko-sroberta-multitask")

# P1 priority laws
P1_LAWS = ["민법", "의료법", "근로기준법", "형법", "자본시장법"]

_PAGE_SIZE = 100


def _rate_limited_get(url: str, timeout: int = 30) -> str:
    elapsed = time.time() - _LAST_REQUEST_TIME[0]
    if elapsed < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - elapsed)
    _LAST_REQUEST_TIME[0] = time.time()
    req = urllib.request.urlopen(url, timeout=timeout)
    return req.read().decode("utf-8")


def _search_interpretations(query: str, page: int = 1, display: int = _PAGE_SIZE) -> tuple[int, list[dict]]:
    """
    Search 법령해석례 by keyword.
    Returns (total_count, list of expc dicts).
    """
    params = {
        "OC": _OC,
        "target": "expc",
        "type": "XML",
        "query": query,
        "display": str(display),
        "page": str(page),
    }
    url = f"{_DRF_BASE}/lawSearch.do?" + urllib.parse.urlencode(params)
    logger.debug("Searching expc: %s (page=%d)", query, page)
    xml_text = _rate_limited_get(url)

    root = ET.fromstring(xml_text)
    total = int(root.findtext("totalCnt") or "0")
    results = []
    for expc_el in root.findall("expc"):
        results.append({
            "expc_id": (expc_el.findtext("법령해석례일련번호") or "").strip(),
            "case_title": (expc_el.findtext("안건명") or "").strip(),
            "case_number": (expc_el.findtext("안건번호") or "").strip(),
            "decision_date": (expc_el.findtext("회신일자") or "").strip(),
            "issuer": (expc_el.findtext("회신기관명") or "").strip(),
            "issuer_code": (expc_el.findtext("회신기관코드") or "").strip(),
            "requester": (expc_el.findtext("질의기관명") or "").strip(),
            "requester_code": (expc_el.findtext("질의기관코드") or "").strip(),
            "detail_url": (expc_el.findtext("법령해석례상세링크") or "").strip(),
        })
    return total, results


def _clean_html(text: str | None) -> str:
    """Strip HTML tags from CDATA text."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _fetch_interpretation_detail(expc_id: str) -> dict:
    """
    Fetch full 법령해석례 detail (질의요지 + 회답 + 이유) via XML.
    Returns dict with those keys (may be empty strings).
    """
    url = f"{_DRF_BASE}/lawService.do?OC={_OC}&target=expc&ID={expc_id}&type=XML"
    logger.debug("Fetching expc detail ID=%s", expc_id)
    xml_text = _rate_limited_get(url)

    root = ET.fromstring(xml_text)

    # Normalize date to YYYYMMDD
    decision_date = (root.findtext("해석일자") or "").strip().replace(".", "").replace("-", "")

    return {
        "decision_date": decision_date,
        "issuer": (root.findtext("해석기관명") or "").strip(),
        "issuer_code": (root.findtext("해석기관코드") or "").strip(),
        "requester": (root.findtext("질의기관명") or "").strip(),
        "requester_code": (root.findtext("질의기관코드") or "").strip(),
        "질의요지": _clean_html(root.findtext("질의요지")),
        "회답": _clean_html(root.findtext("회답")),
        "이유": _clean_html(root.findtext("이유")),
    }


def _build_document(search_item: dict, detail: dict) -> str:
    """Build ChromaDB document text from search + detail data."""
    issuer = detail.get("issuer") or search_item.get("issuer") or ""
    case_num = search_item["case_number"]
    decision_date = detail.get("decision_date") or ""
    # Format date as YYYY.MM.DD for display
    if len(decision_date) == 8:
        display_date = f"{decision_date[:4]}.{decision_date[4:6]}.{decision_date[6:]}"
    else:
        display_date = search_item.get("decision_date") or ""

    case_title = search_item["case_title"]
    질의요지 = detail.get("질의요지", "")
    회답 = detail.get("회답", "")
    이유 = detail.get("이유", "")

    header = f"[{issuer} {case_num} ({display_date})] {case_title}"
    parts = [header]
    if 질의요지:
        parts.append(f"\n질의요지:\n{질의요지}")
    if 회답:
        parts.append(f"\n회답:\n{회답}")
    if 이유:
        # Truncate 이유 to keep doc within limits
        parts.append(f"\n이유:\n{이유[:3000]}")

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


def ingest_interpretations(
    laws: list[str] | None = None,
    max_per_law: int | None = None,
    collection_name: str = _COLLECTION_NAME,
    persist_path: str = _CHROMA_PERSIST,
    embedding_function=None,
    dry_run: bool = False,
) -> dict:
    """
    Ingest 법령해석례 into kolaw_interpretations ChromaDB collection.

    Returns:
        dict with keys: laws_processed, docs_ingested, docs_skipped, errors, collection
    """
    if laws is None:
        laws = P1_LAWS

    client = None
    collection = None

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

        all_results: list[dict] = []
        while True:
            total_count, results = _search_interpretations(law_name, page=page)
            if not results:
                break
            all_results.extend(results)
            logger.info("  Page %d: got %d (total=%d so far=%d)",
                        page, len(results), total_count, len(all_results))
            if len(all_results) >= total_count:
                break
            if max_per_law and len(all_results) >= max_per_law:
                break
            if len(results) < _PAGE_SIZE:
                break
            page += 1

        if max_per_law:
            all_results = all_results[:max_per_law]

        logger.info("  Total to process for %s: %d", law_name, len(all_results))

        if collection is not None:
            candidate_ids = [f"expc_{r['expc_id']}" for r in all_results if r["expc_id"]]
            try:
                existing = collection.get(ids=candidate_ids, include=[])
                existing_ids = set(existing["ids"])
            except Exception:
                existing_ids = set()
        else:
            existing_ids = set()

        for item in all_results:
            expc_id = item["expc_id"]
            if not expc_id:
                continue

            doc_id = f"expc_{expc_id}"

            if doc_id in existing_ids:
                law_skipped += 1
                continue

            if dry_run:
                if law_ingested < 3:
                    print(f"  [dry_run] {doc_id}: {item['case_title'][:80]}")
                law_ingested += 1
                continue

            try:
                detail = _fetch_interpretation_detail(expc_id)
            except Exception as exc:
                logger.warning("  Detail fetch failed for %s: %s", expc_id, exc)
                detail = {}
                total_errors += 1

            doc_text = _build_document(item, detail)

            decision_date = detail.get("decision_date", "")
            if not decision_date:
                # Fall back to search result date (may be YYYY.MM.DD format)
                raw = item.get("decision_date", "")
                decision_date = raw.replace(".", "").replace("-", "")

            metadata = {
                "expc_id": str(expc_id),
                "case_title": item["case_title"][:500],
                "case_number": item["case_number"],
                "decision_date": decision_date,
                "issuer": (detail.get("issuer") or item.get("issuer") or "").strip(),
                "issuer_code": (detail.get("issuer_code") or item.get("issuer_code") or "").strip(),
                "requester": (detail.get("requester") or item.get("requester") or "").strip(),
                "requester_code": (detail.get("requester_code") or item.get("requester_code") or "").strip(),
                "related_law": law_name,
                "detail_url": item.get("detail_url", ""),
                "ingested_at": ingested_at,
            }

            collection.add(
                ids=[doc_id],
                documents=[doc_text],
                metadatas=[metadata],
            )
            law_ingested += 1

            if law_ingested % 10 == 0:
                print(
                    f"[ingest_interpretations] {law_name}: {law_ingested} ingested, "
                    f"{law_skipped} skipped so far",
                    flush=True,
                )

        total_ingested += law_ingested
        total_skipped += law_skipped
        laws_processed += 1
        print(
            f"[ingest_interpretations] {law_name} done: "
            f"ingested={law_ingested} skipped={law_skipped}",
            flush=True,
        )

    print(
        f"[ingest_interpretations] DONE: {laws_processed} laws, "
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

    parser = argparse.ArgumentParser(description="Ingest 법령해석례 from law.go.kr DRF into kolaw_interpretations")
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
        help="Max interpretations per law (default: all)",
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

    result = ingest_interpretations(
        laws=args.laws,
        max_per_law=args.max_per_law,
        collection_name=args.collection,
        persist_path=args.persist,
        dry_run=args.dry_run,
    )
    print(f"Result: {result}")
