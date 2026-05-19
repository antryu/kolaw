"""
Delegation-chain lookup over the crossref sidecar index.

Phase 1 built ``services/crossref/index/<법령폴더>.json`` — one sidecar file
per law, each holding the law's delegation chains (본법 조문 → 시행령/시행규칙
조문 → 별표). The chains are keyed by the SAME ChromaDB doc_id scheme that
``services/fast_search/ingest_legalize_kr._law_docs`` produces, so a search /
article hit's doc_id can be matched directly onto a chain.

This module loads those sidecars ONCE (process start, lazy) into an in-memory
``doc_id -> chain`` dict and exposes O(1) lookups.

Design notes
------------
* 1,280 sidecar files / ~39 MB. Loaded once into memory on first lookup
  (lazy) and cached for the process lifetime — never re-read per request.
* A chain is reachable from ANY of its member articles: the primary-law
  article (``law_doc_id``), every 시행령 article (``decree_articles[].doc_id``),
  every 시행규칙 article (``rule_articles[].doc_id``), and any 시행규칙 article
  nested under a 시행령 article. The index maps each of those doc_ids to the
  same chain object, so a hit on a 시행령 조문 surfaces its 본법 parent too.
* 별표 carry no doc_id (별표 본문이 코퍼스에 없음 — law.go.kr catalogue only),
  so they are exposed only as the ``byeolpyo`` field already on the chain.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# services/crossref/index/ — sidecar JSON dir produced by build_index.py.
_INDEX_DIR = Path(__file__).resolve().parent / "index"

# doc_id -> chain dict (the chain as stored in the sidecar JSON, lightly
# normalized for API consumption). None until first load.
_CHAIN_BY_DOC_ID: dict[str, dict] | None = None

# (law_id, file_type, canonical_article) -> chain dict. Secondary index for
# callers (e.g. /article) that hold a canonical 조문 ref but not a doc_id.
# file_type is one of 법률 / 시행령 / 시행규칙.
_CHAIN_BY_ARTICLE: dict[tuple[str, str, str], dict] | None = None

_LOAD_LOCK = threading.Lock()


def _normalize_chain(chain: dict, law_name: str, law_id: str) -> dict:
    """
    Lightly reshape a raw sidecar chain into an API-friendly dict.

    Keeps the index structure but adds the owning law's name/id so a caller
    holding only one chain still knows which law it belongs to.
    """
    return {
        "law_name": law_name,
        "law_id": law_id,
        "law_article": chain.get("law_article", ""),
        "law_doc_id": chain.get("law_doc_id", ""),
        "law_title": chain.get("law_title", ""),
        "delegation_kind": chain.get("delegation_kind", []),
        "byeolpyo": chain.get("byeolpyo", []),
        "decree_articles": chain.get("decree_articles", []),
        "rule_articles": chain.get("rule_articles", []),
    }


def _index_chain_members(
    chain: dict,
    normalized: dict,
    law_id: str,
    by_doc_id: dict[str, dict],
    by_article: dict[tuple[str, str, str], dict],
) -> None:
    """Register every doc_id / (law_id, file_type, article) of `chain`."""
    # Primary-law article — build_index always parses 법률.md as the primary.
    law_doc_id = chain.get("law_doc_id")
    if law_doc_id:
        by_doc_id.setdefault(law_doc_id, normalized)
    law_article = chain.get("law_article")
    if law_article:
        by_article.setdefault((law_id, "법률", law_article), normalized)

    for decree in chain.get("decree_articles", []) or []:
        d_id = decree.get("doc_id")
        if d_id:
            by_doc_id.setdefault(d_id, normalized)
        d_article = decree.get("article")
        d_ftype = decree.get("file_type", "시행령")
        if d_article:
            by_article.setdefault((law_id, d_ftype, d_article), normalized)
        # 시행규칙 article(s) nested under a 시행령 article (build_index adds
        # this key only when present).
        for sub_rule in decree.get("rule_articles", []) or []:
            sr_id = sub_rule.get("doc_id")
            if sr_id:
                by_doc_id.setdefault(sr_id, normalized)
            sr_article = sub_rule.get("article")
            sr_ftype = sub_rule.get("file_type", "시행규칙")
            if sr_article:
                by_article.setdefault((law_id, sr_ftype, sr_article), normalized)

    for rule in chain.get("rule_articles", []) or []:
        r_id = rule.get("doc_id")
        if r_id:
            by_doc_id.setdefault(r_id, normalized)
        r_article = rule.get("article")
        r_ftype = rule.get("file_type", "시행규칙")
        if r_article:
            by_article.setdefault((law_id, r_ftype, r_article), normalized)


def _build_tables() -> tuple[dict[str, dict], dict[tuple[str, str, str], dict]]:
    """Read every sidecar JSON; build doc_id and (law_id,type,article) tables."""
    by_doc_id: dict[str, dict] = {}
    by_article: dict[tuple[str, str, str], dict] = {}
    if not _INDEX_DIR.is_dir():
        logger.warning("crossref index dir not found: %s", _INDEX_DIR)
        return by_doc_id, by_article

    files = sorted(_INDEX_DIR.glob("*.json"))
    loaded = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("crossref index skip %s: %s", path.name, exc)
            continue
        law_name = data.get("law_name", "") or data.get("law_folder", "")
        law_id = data.get("law_id", "")
        for chain in data.get("delegation_chains", []) or []:
            normalized = _normalize_chain(chain, law_name, law_id)
            _index_chain_members(chain, normalized, law_id, by_doc_id, by_article)
        loaded += 1

    logger.info(
        "crossref lookup: %d laws loaded, %d doc_ids / %d articles indexed",
        loaded, len(by_doc_id), len(by_article),
    )
    return by_doc_id, by_article


def _ensure_loaded() -> None:
    """Build both lookup tables once on first call (thread-safe)."""
    global _CHAIN_BY_DOC_ID, _CHAIN_BY_ARTICLE
    if _CHAIN_BY_DOC_ID is None:
        with _LOAD_LOCK:
            if _CHAIN_BY_DOC_ID is None:
                by_doc_id, by_article = _build_tables()
                _CHAIN_BY_ARTICLE = by_article
                _CHAIN_BY_DOC_ID = by_doc_id


def _get_table() -> dict[str, dict]:
    """Return the doc_id -> chain table, building it once on first call."""
    _ensure_loaded()
    assert _CHAIN_BY_DOC_ID is not None
    return _CHAIN_BY_DOC_ID


def get_delegation_chain(doc_id: str | None) -> dict | None:
    """
    Return the delegation chain a `doc_id` belongs to, or None.

    `doc_id` is the ChromaDB id of a law article (본법, 시행령, or 시행규칙).
    Returns the chain dict (see `_normalize_chain`) when the article is part
    of an indexed delegation chain; None when the article delegates nothing
    and is referenced by nothing — callers MUST treat None as "no chain" and
    omit the field, preserving pre-Phase-2 behaviour.
    """
    if not doc_id:
        return None
    return _get_table().get(doc_id)


def get_delegation_chain_by_article(
    law_id: str | None,
    file_type: str | None,
    article: str | None,
) -> dict | None:
    """
    Return the delegation chain a (law_id, file_type, article) belongs to.

    Secondary lookup for callers that hold a canonical 조문 ref but not a
    ChromaDB doc_id — e.g. the /article endpoint, whose `lookup_article` gives
    a normalized article like '제28조의8' plus law_id and 법령 종류.

    `file_type` must be one of 법률 / 시행령 / 시행규칙. Returns None when the
    triple is not part of any indexed chain.
    """
    if not (law_id and file_type and article):
        return None
    _ensure_loaded()
    assert _CHAIN_BY_ARTICLE is not None
    return _CHAIN_BY_ARTICLE.get((law_id, file_type, article))


def warm() -> int:
    """
    Eagerly build the lookup tables (e.g. from FastAPI lifespan warmup).

    Returns the number of indexed doc_ids. Safe to call repeatedly.
    """
    return len(_get_table())
