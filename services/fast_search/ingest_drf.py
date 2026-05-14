"""
DRF API ingestion — fetches laws missing from legalize-kr corpus via law.go.kr DRF.

Used for corpus_gap laws (e.g. 형법) that don't appear in legalize-kr/kr/.

Source: law.go.kr DRF XML API
  Search: GET /DRF/lawSearch.do?OC=Hydrogen&target=law&type=XML&query=<name>
  Detail: GET /DRF/lawService.do?OC=Hydrogen&target=law&MST=<MST>&type=XML

Rate limit: 1 req/sec (polite; law.go.kr TOS)

Usage:
  CHROMA_PERSIST_PATH=services/fast_search/chroma_db \\
  KOLAW_COLLECTION=kolaw_laws_v2 \\
  python -m services.fast_search.ingest_drf --law 형법

  python -m services.fast_search.ingest_drf --law 민법 --collection kolaw_laws_v2
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
from typing import Iterator

logger = logging.getLogger(__name__)

_OC = os.getenv("LAW_GO_KR_OC", "Hydrogen")
_DRF_BASE = "https://www.law.go.kr/DRF"
_RATE_LIMIT_SEC = 1.0  # 1 req/sec — polite
_LAST_REQUEST_TIME = [0.0]

_CHROMA_PERSIST = os.getenv(
    "CHROMA_PERSIST_PATH",
    str(Path(__file__).parent / "chroma_db"),
)
_COLLECTION_NAME = os.getenv("KOLAW_COLLECTION", "kolaw_laws")
_EMBEDDING_MODEL = os.getenv("KOLAW_EMBEDDING_MODEL", "jhgan/ko-sroberta-multitask")


def _rate_limited_get(url: str, timeout: int = 30) -> str:
    """Fetch URL with 1 req/sec rate limit."""
    elapsed = time.time() - _LAST_REQUEST_TIME[0]
    if elapsed < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - elapsed)
    _LAST_REQUEST_TIME[0] = time.time()
    req = urllib.request.urlopen(url, timeout=timeout)
    return req.read().decode("utf-8")


def _search_law(query: str) -> list[dict]:
    """
    Search law.go.kr for laws matching query.
    Returns list of {law_name, law_id, mst, enforcement_date, type} dicts.
    """
    params = {
        "OC": _OC,
        "target": "law",
        "type": "XML",
        "query": query,
        "display": "10",
        "page": "1",
    }
    url = f"{_DRF_BASE}/lawSearch.do?" + urllib.parse.urlencode(params)
    logger.info("Searching: %s", query)
    xml_text = _rate_limited_get(url)

    root = ET.fromstring(xml_text)
    results = []
    for law_el in root.findall("law"):
        law_name = (law_el.findtext("법령명한글") or "").strip()
        law_id = (law_el.findtext("법령ID") or "").strip()
        mst = (law_el.findtext("법령일련번호") or "").strip()
        ef_date = (law_el.findtext("시행일자") or "").strip()
        law_type = (law_el.findtext("법령구분명") or "").strip()
        results.append({
            "law_name": law_name,
            "law_id": law_id,
            "mst": mst,
            "enforcement_date": ef_date,
            "law_type": law_type,
        })
    return results


def _find_exact_law(query: str) -> dict | None:
    """Find exact law by name. Returns best match dict or None."""
    results = _search_law(query)
    # Exact match first
    for r in results:
        if r["law_name"] == query and r["law_type"] == "법률":
            return r
    # Partial match (law_name contains query)
    for r in results:
        if query in r["law_name"] and r["law_type"] == "법률":
            return r
    return results[0] if results else None


def _fetch_law_xml(mst: str) -> str:
    """Fetch full law XML by MST (법령일련번호)."""
    url = f"{_DRF_BASE}/lawService.do?OC={_OC}&target=law&MST={mst}&type=XML"
    logger.info("Fetching law MST=%s", mst)
    return _rate_limited_get(url)


def _xml_to_article_chunks(xml_text: str, law_name: str, law_id: str, enforcement_date: str) -> Iterator[tuple[str, dict, str]]:
    """
    Parse DRF XML into article chunks compatible with legalize-kr schema.
    Yields (doc_id, metadata, content).
    """
    root = ET.fromstring(xml_text)
    ingested_at = datetime.now(timezone.utc).isoformat()

    # Determine doc_type from 법종구분
    법종구분 = ""
    법종코드 = ""
    기본정보 = root.find("기본정보")
    if 기본정보 is not None:
        elem = 기본정보.find("법종구분")
        if elem is not None:
            법종코드 = elem.get("법종구분코드", "")
            법종구분 = elem.text or ""

    # Map 법종구분코드 to file_type labels matching legalize-kr schema
    type_map = {
        "A0002": "법률",       # 법률
        "A0003": "시행령",     # 대통령령
        "A0004": "시행규칙",   # 부령/규칙
        "A0005": "고시",       # 고시/훈령
    }
    file_type = type_map.get(법종코드, "법률")

    조문단위_list = root.findall(".//조문단위")
    folder_slug = re.sub(r"\s+", "_", law_name)[:20]
    seen: set[str] = set()

    for 조문단위 in 조문단위_list:
        번호 = (조문단위.findtext("조문번호") or "").strip()
        if not 번호:
            continue

        # Build 조항 text from 항 elements if present
        항_list = 조문단위.findall(".//항")
        if 항_list:
            parts = []
            for 항 in 항_list:
                항_content = (항.findtext("항내용") or "").strip()
                if 항_content:
                    parts.append(항_content)
                # 호 sub-items
                for 호 in 항.findall(".//호"):
                    호_content = (호.findtext("호내용") or "").strip()
                    if 호_content:
                        parts.append("  " + 호_content)
            content = "\n".join(parts)
        else:
            content = (조문단위.findtext("조문내용") or "").strip()

        if not content or len(content.strip()) < 5:
            continue

        # Extract article title from 조문제목 field (e.g. "정당방위", "연차 유급휴가")
        article_title = (조문단위.findtext("조문제목") or "").strip()

        # Normalize article number
        try:
            article_int = int(번호)
            article_num = f"제{article_int}조"
        except ValueError:
            article_num = f"제{번호}조"

        doc_id = f"{law_id}_{folder_slug}_{file_type}_{article_num}"
        # Deduplicate
        if doc_id in seen:
            suffix = 2
            while f"{doc_id}_{suffix}" in seen:
                suffix += 1
            doc_id = f"{doc_id}_{suffix}"
        seen.add(doc_id)

        # Include article_title in head so BM25/vector can match title keywords
        # e.g. 형법 §21 head = "[형법 제21조 (정당방위)] " → "정당방위" searchable
        title_part = f" ({article_title})" if article_title else ""
        head = f"[{law_name} {article_num}{title_part}] "
        full_content = (head + content)[:8000]

        metadata = {
            "law_id": law_id,
            "law_name": law_name,
            "law_folder": law_name,
            "file_type": file_type,
            "article": article_num,
            "article_title": article_title,
            "source_file": f"drf_api:MST/{law_id}",
            "enforcement_date": enforcement_date,
            "ingested_at": ingested_at,
        }

        yield doc_id, metadata, full_content


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


def ingest_law_from_drf(
    law_query: str,
    collection_name: str = _COLLECTION_NAME,
    persist_path: str = _CHROMA_PERSIST,
    embedding_function=None,
    dry_run: bool = False,
) -> dict:
    """
    Fetch a single law from law.go.kr DRF and ingest into ChromaDB.

    Returns: {"law_name": str, "law_id": str, "docs_ingested": int, "docs_skipped": int}
    """
    # Find law
    law_info = _find_exact_law(law_query)
    if not law_info:
        raise ValueError(f"Law not found on law.go.kr: {law_query!r}")

    law_name = law_info["law_name"]
    law_id = law_info["law_id"]
    mst = law_info["mst"]
    enforcement_date = law_info["enforcement_date"]

    logger.info("Found: %s (ID=%s, MST=%s)", law_name, law_id, mst)

    # Fetch full XML
    xml_text = _fetch_law_xml(mst)

    # Parse into chunks
    chunks = list(_xml_to_article_chunks(xml_text, law_name, law_id, enforcement_date))
    logger.info("Parsed %d article chunks from %s", len(chunks), law_name)

    if dry_run:
        for doc_id, meta, content in chunks[:3]:
            print(f"  [dry_run] {doc_id}: {content[:100]}")
        return {"law_name": law_name, "law_id": law_id, "docs_ingested": 0, "docs_skipped": len(chunks)}

    # Ingest into ChromaDB
    client = get_chroma_client(persist_path)
    ef = embedding_function or get_embedding_function()
    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=ef,
    )

    batch_ids = [doc_id for doc_id, _, _ in chunks]
    batch_docs = [content for _, _, content in chunks]
    batch_metas = [meta for _, meta, _ in chunks]

    # Dedup check
    try:
        existing = collection.get(ids=batch_ids, include=[])
        existing_ids = set(existing["ids"])
    except Exception:
        existing_ids = set()

    new_ids = [i for i in batch_ids if i not in existing_ids]
    new_docs = [d for i, d in zip(batch_ids, batch_docs) if i not in existing_ids]
    new_metas = [m for i, m in zip(batch_ids, batch_metas) if i not in existing_ids]

    docs_skipped = len(batch_ids) - len(new_ids)
    docs_ingested = 0
    if new_ids:
        # Ingest in batches of 128
        for start in range(0, len(new_ids), 128):
            end = start + 128
            collection.add(
                ids=new_ids[start:end],
                documents=new_docs[start:end],
                metadatas=new_metas[start:end],
            )
            docs_ingested += len(new_ids[start:end])

    logger.info("Ingested %d, skipped %d for %s", docs_ingested, docs_skipped, law_name)
    return {
        "law_name": law_name,
        "law_id": law_id,
        "docs_ingested": docs_ingested,
        "docs_skipped": docs_skipped,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Ingest a single law from law.go.kr DRF API")
    parser.add_argument("--law", required=True, help="Law name to search and ingest (e.g. 형법)")
    parser.add_argument("--collection", default=_COLLECTION_NAME, help=f"ChromaDB collection name (default: {_COLLECTION_NAME})")
    parser.add_argument("--persist", default=_CHROMA_PERSIST, help=f"ChromaDB persist path (default: {_CHROMA_PERSIST})")
    parser.add_argument("--dry-run", action="store_true", help="Print first 3 chunks, don't ingest")
    args = parser.parse_args()

    result = ingest_law_from_drf(
        law_query=args.law,
        collection_name=args.collection,
        persist_path=args.persist,
        dry_run=args.dry_run,
    )
    print(f"Result: {result}")
