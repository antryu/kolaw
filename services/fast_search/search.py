"""
ChromaDB fast search path — Y option hybrid retrieval.

Retrieval stack (applied in order):
  1. law_id boost: if query contains a known 법령명, results from that law get +0.3
  2. BM25 + vector hybrid via Reciprocal Rank Fusion (RRF, k=60)
  3. LLM rerank: mode=fast → qwen3 local, mode=deep → Claude Opus 4.7

Handles 90% of queries via vector similarity on legalize-kr ChromaDB.
Returns structured Citations compatible with kolaw SearchResponse.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from apps.api.schemas import Citation, SearchRequest, SearchResponse
from services.fast_search.ingest import (
    _COLLECTION_NAME,
    get_chroma_client,
    get_embedding_function,
    ingest,
)

logger = logging.getLogger(__name__)

_TOP_K = 5
_HYBRID_PULL_K = 20  # candidates pulled from each of vector + BM25 before RRF
_RRF_K = 60          # standard RRF constant
_FAST_RERANK_K = 5   # fast mode: limit rerank candidates (latency budget)
_COLLECTION_CACHE: dict = {}
_BM25_TOKENIZER_VERSION = 2  # increment when tokenizer logic changes
_BM25_CACHE: dict = {}  # {(collection_count, tokenizer_version): ...}

# --- Law-name extraction helpers ---

_LAW_SUFFIX_RE = re.compile(r"(법률|시행령|시행규칙|법|규정|규칙)$")
_HANGUL_TOKEN_RE = re.compile(r"[가-힣]{2,}")

# Canonical 법령명 → law_id boost aliases (covers major laws; extendable)
_LAW_NAME_ALIASES: dict[str, list[str]] = {
    "의료법": ["의료법"],
    "근로기준법": ["근로기준법"],
    "민법": ["민법"],
    "형법": ["형법"],
    "상법": ["상법"],
    "행정소송법": ["행정소송법"],
    "행정절차법": ["행정절차법"],
    "국가공무원법": ["국가공무원법"],
    "소득세법": ["소득세법"],
    "법인세법": ["법인세법"],
    "부가가치세법": ["부가가치세법"],
    "국민건강보험법": ["국민건강보험법"],
    "산업재해보상보험법": ["산업재해보상보험법"],
    "고용보험법": ["고용보험법"],
    "국민연금법": ["국민연금법"],
    "정보통신망법": ["정보통신망법"],
    "개인정보보호법": ["개인정보보호법"],
    "수소법": ["수소경제", "수소 안전"],
    "고압가스법": ["고압가스"],
    "국가계약법": ["국가계약"],
    "전자조달법": ["전자조달"],
    "의료기기법": ["의료기기"],
    "약사법": ["약사"],
    "자본시장법": ["자본시장"],
    # E3 (cycle 3): 4 special law alias
    "가사소송법": ["가사소송"],
    "부동산등기법": ["부동산등기"],
    "신탁법": ["신탁"],
    "상법": ["상법"],
    # Cycle 4: 형사소송법 corpus added (2026-05-15)
    "형사소송법": ["형사소송법", "형소법"],
}


# Map from legalize-kr corpus dir name → canonical law name in _LAW_NAME_ALIASES.
# Used by fast_search to honor an explicit `req.laws=[<dir_name>]` filter even when
# the query itself does not contain a canonical name (e.g. "환자 동의" + laws=["uirobub"]).
_LAW_DIR_TO_CANONICAL: dict[str, str] = {
    "uirobub": "의료법",
    "yaksabub": "약사법",
    "minbub": "민법",
    "hyungbub": "형법",
    "labor": "근로기준법",
    "jabonsijang": "자본시장법",
    "suso": "수소법",
    "gpgas": "고압가스법",
    # E3 (cycle 3): 4 special law
    "가사소송법": "가사소송법",
    "부동산등기법": "부동산등기법",
    "신탁법": "신탁법",
    "상법": "상법",
    # Cycle 4: 형사소송법 corpus added (2026-05-15)
    "형사소송법": "형사소송법",
}


def _extract_law_keyword(query: str) -> str | None:
    """
    Pick a single Korean substring from `query` to use as a `where_document`
    `$contains` filter.

    Strategy:
      1. If query contains a recognized 법령명 AND additional content tokens,
         prefer the longest content token (not the law name) as the filter.
         Rationale: "근로기준법 연차휴가" → use "연차휴가", not "근로기준"
         so the $contains filter finds docs that actually discuss the topic.
      2. Otherwise strip law suffixes (e.g. "수소경제법" → "수소경제") and
         return the longest 3+ char stripped token.
      3. Fall back to the longest 2+ char Hangul token.

    Returns None when the query has no usable Korean tokens.
    """
    tokens = _HANGUL_TOKEN_RE.findall(query)
    if not tokens:
        return None

    # Structural words that are NOT useful as $contains content keywords
    _STRUCTURAL_TOKENS = {
        "시행령", "시행규칙", "규정", "규칙", "법률",
        "요건", "시효", "효력", "책임",
        "보관기간", "보존기간", "처벌규정",  # generic retention/penalty terms
    }

    # Check if any token is or starts with a canonical law name
    canonical = _detect_law_name(query)
    if canonical:
        # Filter out the law-name token(s) and structural tokens
        non_law_tokens = [
            t for t in tokens
            if not (t == canonical or t.startswith(canonical) or canonical.startswith(t))
            and t not in _STRUCTURAL_TOKENS
        ]
        if non_law_tokens:
            # Use the longest non-law token (≥ 2 chars) as the content keyword
            best = max(non_law_tokens, key=len)
            if len(best) >= 2:
                return best

    # No canonical law in query — strip law suffixes from all tokens
    for token in sorted(tokens, key=len, reverse=True):
        stripped = _LAW_SUFFIX_RE.sub("", token)
        if len(stripped) >= 3:
            return stripped
    return max(tokens, key=len)


def _detect_law_name(query: str) -> str | None:
    """
    Check if query contains a known canonical 법령명.
    Returns the matched canonical name (e.g. "의료법") or None.
    """
    for canonical in _LAW_NAME_ALIASES:
        if canonical in query:
            return canonical
    return None


def _get_collection():
    client = get_chroma_client()
    ef = get_embedding_function()
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef,
    )
    if collection.count() == 0:
        logger.info("Collection empty — auto-ingesting fixture data")
        ingest()
    return collection


# --- BM25 index (lazy, cached by collection count) ---

def _get_bm25_index(collection):
    """
    Build (or return cached) BM25 index over all ChromaDB documents.
    Cache key = collection.count() — invalidates on re-ingest.

    Returns (bm25, doc_ids, metadatas, documents) or None on failure.
    """
    count = collection.count()
    cache_key = (count, _BM25_TOKENIZER_VERSION)
    if cache_key in _BM25_CACHE:
        return _BM25_CACHE[cache_key]

    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        logger.warning("rank_bm25 not installed — BM25 disabled")
        _BM25_CACHE[count] = None
        return None

    logger.info("Building BM25 index over %d documents (one-time, cached)...", count)
    t0 = time.time()
    try:
        # Fetch all docs in batches of 10000 (ChromaDB limit per call)
        all_ids: list[str] = []
        all_docs: list[str] = []
        all_metas: list[dict] = []
        batch_size = 5000
        offset = 0
        while True:
            batch = collection.get(
                limit=batch_size,
                offset=offset,
                include=["documents", "metadatas"],
            )
            ids = batch.get("ids", [])
            if not ids:
                break
            all_ids.extend(ids)
            all_docs.extend(batch.get("documents", []) or [])
            all_metas.extend(batch.get("metadatas", []) or [])
            offset += len(ids)
            if len(ids) < batch_size:
                break

        def _tokenize(text: str) -> list[str]:
            """
            Korean-aware tokenizer for BM25.
            Produces word-level tokens + 2-char Hangul bigrams.
            Bigrams handle compound terms like "연차휴가" matching "연차" + "휴가"
            when the document stores them as "연차 유급휴가" (space-separated).
            """
            text = text or ""
            word_tokens = re.findall(r"[가-힣]+|[a-zA-Z0-9]{2,}", text.lower())
            bigrams: list[str] = []
            for tok in word_tokens:
                if len(tok) >= 2:
                    for i in range(len(tok) - 1):
                        bigrams.append(tok[i:i+2])
            combined = word_tokens + bigrams
            return combined if combined else ["_empty_"]

        tokenized = [_tokenize(d) for d in all_docs]
        bm25 = BM25Okapi(tokenized)
        result = (bm25, all_ids, all_metas, all_docs, tokenized)
        _BM25_CACHE[cache_key] = result
        logger.info("BM25 index built in %.1fs (%d docs)", time.time() - t0, len(all_ids))
        return result
    except Exception as exc:
        logger.warning("BM25 index build failed: %s — vector-only fallback", exc)
        _BM25_CACHE[cache_key] = None
        return None


def _bm25_search(
    query: str,
    collection,
    n: int,
    law_name_filter: str | None,
) -> list[tuple[str, dict, str]]:
    """
    Run BM25 search, return top-n as [(doc_id, metadata, document)].
    If law_name_filter is set, only considers chunks from that law.
    """
    index_data = _get_bm25_index(collection)
    if index_data is None:
        return []

    bm25, all_ids, all_metas, all_docs, _ = index_data

    def _tokenize(text: str) -> list[str]:
        text = text or ""
        word_tokens = re.findall(r"[가-힣]+|[a-zA-Z0-9]{2,}", text.lower())
        bigrams: list[str] = []
        for tok in word_tokens:
            if len(tok) >= 2:
                for i in range(len(tok) - 1):
                    bigrams.append(tok[i:i+2])
        combined = word_tokens + bigrams
        return combined if combined else ["_empty_"]

    query_tokens = _tokenize(query)
    scores = bm25.get_scores(query_tokens)

    # Filter by law_name if specified — exact folder match preferred to avoid
    # substring false positives (e.g. "민법" matching "난민법", "상법" matching "관세법 시행규칙").
    if law_name_filter:
        filtered = [
            (i, scores[i])
            for i, m in enumerate(all_metas)
            if (
                m.get("law_folder", "") == law_name_filter
                or m.get("law_name", "") == law_name_filter
                or m.get("law_name", "").startswith(law_name_filter + " ")
                or m.get("law_folder", "").startswith(law_name_filter + " ")
            )
        ]
    else:
        filtered = list(enumerate(scores))

    # Sort by score desc, take top-n
    filtered.sort(key=lambda x: x[1], reverse=True)
    top = filtered[:n]

    return [
        (all_ids[i], all_metas[i] or {}, all_docs[i] or "")
        for i, _ in top
    ]


def _rrf_fuse(
    vector_hits: list[tuple[str, dict, str, float]],  # (id, meta, doc, distance)
    bm25_hits: list[tuple[str, dict, str]],            # (id, meta, doc)
    top_k: int,
    k: int = _RRF_K,
) -> list[tuple[str, dict, str, float]]:
    """
    Reciprocal Rank Fusion of vector + BM25 results.
    Returns top_k items as (id, meta, doc, rrf_score) sorted desc.
    """
    scores: dict[str, float] = {}
    id_to_data: dict[str, tuple[dict, str, float]] = {}

    for rank, (doc_id, meta, doc, dist) in enumerate(vector_hits):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        id_to_data[doc_id] = (meta, doc, dist)

    for rank, (doc_id, meta, doc) in enumerate(bm25_hits):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        if doc_id not in id_to_data:
            id_to_data[doc_id] = (meta, doc, 1.0)

    sorted_ids = sorted(scores, key=lambda d: scores[d], reverse=True)[:top_k]
    return [
        (doc_id, id_to_data[doc_id][0], id_to_data[doc_id][1], scores[doc_id])
        for doc_id in sorted_ids
    ]


def _law_id_boost(
    hits: list[tuple[str, dict, str, float]],
    canonical_law_name: str | None,
    boost: float = 0.3,
) -> list[tuple[str, dict, str, float]]:
    """
    If query contains a known 법령명, boost RRF scores for chunks from that law.
    Returns re-sorted list.
    """
    if not canonical_law_name:
        return hits

    boosted = []
    for doc_id, meta, doc, score in hits:
        law_name = meta.get("law_name", "") or ""
        law_folder = meta.get("law_folder", "") or ""
        # Match: law is the canonical law or its 시행령/시행규칙
        # Use exact folder match or law_name starts-with to avoid substring collisions
        # (e.g. "민법" must not boost "난민법")
        aliases = _LAW_NAME_ALIASES.get(canonical_law_name, [canonical_law_name])
        is_match = (
            law_folder == canonical_law_name
            or law_name == canonical_law_name
            or law_folder.startswith(canonical_law_name + " ")
            or law_name.startswith(canonical_law_name + " ")
            or any(alias in law_name for alias in aliases if len(alias) >= 4)
        )
        if is_match:
            score += boost
        boosted.append((doc_id, meta, doc, score))

    boosted.sort(key=lambda x: x[3], reverse=True)
    return boosted


async def _llm_rerank(
    query: str,
    hits: list[tuple[str, dict, str, float]],
    mode: str,
    top_k: int,
) -> list[tuple[str, dict, str, float]]:
    """
    LLM rerank of candidates using mode-based routing.
    mode=fast → qwen3 local, mode=deep → Claude Opus 4.7.
    Falls back to original order on any failure.

    Only sends the top (top_k * 2) candidates to LLM — low-score tail items
    (from BM25-only hits not appearing in vector results) are excluded to
    prevent the LLM from promoting tangentially-related laws over the
    authoritative target law.
    """
    if not hits:
        return hits

    # Clip candidates:
    # - fast mode: use _FAST_RERANK_K (5) to stay under 5s latency budget
    # - deep mode: use top_k * 2 (10) for higher accuracy (Opus rerank is fast enough)
    if mode == "fast":
        rerank_n = _FAST_RERANK_K
    else:
        rerank_n = top_k * 2

    rerank_candidates = hits[:rerank_n]

    try:
        from services.llm.router import complete_rerank

        # Compact prompt format for fast mode (fewer tokens = lower latency).
        # /no_think disables Qwen3 extended thinking — avoids token exhaustion during reasoning.
        if mode == "fast":
            lines = []
            for i, (_, meta, doc, _score) in enumerate(rerank_candidates):
                law = meta.get("law_name", "")
                art = meta.get("article", "")
                snip = doc[:150].replace("\n", " ")
                lines.append(f"{i+1}. [{law} {art}] {snip}")
            candidates_text = "\n".join(lines)
            prompt = (
                "/no_think "
                f"Query: {query}\n\n"
                f"Candidates:\n{candidates_text}\n\n"
                "Return JSON array of rank numbers, most relevant first. Example: [3,1,2]\n"
                "JSON only:"
            )
        else:
            candidates_json = json.dumps(
                [
                    {
                        "rank": i + 1,
                        "law_name": meta.get("law_name", ""),
                        "article": meta.get("article", ""),
                        "excerpt": doc[:300],
                    }
                    for i, (_, meta, doc, _score) in enumerate(rerank_candidates)
                ],
                ensure_ascii=False,
                indent=2,
            )
            prompt = (
                f"You are a Korean legal search reranker.\n\n"
                f"Query: {query}\n\n"
                f"Candidates (ranked by hybrid score):\n{candidates_json}\n\n"
                "Return a JSON array of rank numbers in relevance order, most relevant first.\n"
                "Example: [3, 1, 5, 2, 4]\n"
                "Output JSON only, no explanation."
            )

        response = await complete_rerank(
            messages=[{"role": "user", "content": prompt}],
            mode=mode,
            max_tokens=128,
            temperature=0.0,
        )

        # Parse rank list from response
        match = re.search(r"\[[\d,\s]+\]", response)
        if match:
            rank_order = json.loads(match.group())
            # rank_order contains 1-based ranks into rerank_candidates
            reranked = []
            used = set()
            for r in rank_order:
                idx = r - 1
                if 0 <= idx < len(rerank_candidates) and idx not in used:
                    reranked.append(rerank_candidates[idx])
                    used.add(idx)
            # Append any remaining from candidates not selected by LLM
            for i, h in enumerate(rerank_candidates):
                if i not in used:
                    reranked.append(h)
            return reranked[:top_k]

    except Exception as exc:
        logger.warning("LLM rerank failed (%s) — using hybrid order", exc)

    return rerank_candidates[:top_k]


def _meta_to_citation(doc: str, meta: dict, score: float) -> Citation:
    """Convert a retrieval result to a Citation."""
    law_id = meta.get("law_id", "unknown")
    law_name = meta.get("law_name", "")
    article_number = meta.get("article") or meta.get("article_number", "")
    enforcement_date = meta.get("enforcement_date", "")

    match = re.search(r"(\d+)", article_number)
    article_ref = f"§{match.group(1)}" if match else article_number

    excerpt = doc[:400].replace("\n", " ") if doc else ""

    return Citation(
        law_id=law_id,
        law_name=law_name,
        article=article_ref,
        version=enforcement_date.replace("-", "") if enforcement_date else "",
        excerpt=excerpt,
    )


async def fast_search(req: SearchRequest) -> SearchResponse:
    """
    Hybrid vector + BM25 search with law_id boost and LLM rerank.

    Pipeline:
      1. law_id boost detection (query contains canonical 법령명?)
      2. Vector search via ChromaDB (top _HYBRID_PULL_K candidates)
      3. BM25 search (top _HYBRID_PULL_K candidates)
      4. RRF fusion
      5. law_id boost on fused scores
      6. LLM rerank (mode=fast: qwen3, mode=deep: Opus 4.7)
      7. Return top _TOP_K as Citations
    """
    try:
        collection = _get_collection()
        cap = collection.count()
        n_pull = min(_HYBRID_PULL_K, cap)

        # Detect canonical law name for boost
        canonical_law = _detect_law_name(req.query)
        # If caller passed an explicit req.laws filter, prefer it over query-alias
        # detection. This is what makes /search?laws=[<dir>] actually scope the
        # main success path (previously req.laws was only honored in the grep_search
        # exception fallback). Map dir → canonical so downstream BM25/boost work.
        if req.laws:
            for dir_name in req.laws:
                mapped = _LAW_DIR_TO_CANONICAL.get(dir_name)
                if mapped is None and dir_name in _LAW_NAME_ALIASES:
                    mapped = dir_name
                if mapped:
                    canonical_law = mapped
                    break
        keyword = _extract_law_keyword(req.query)

        # --- 1. Vector search ---
        vector_results = None
        if keyword:
            try:
                filtered = collection.query(
                    query_texts=[req.query],
                    n_results=n_pull,
                    where_document={"$contains": keyword},
                )
                if filtered.get("ids", [[]])[0]:
                    vector_results = filtered
                    logger.info(
                        "hybrid vector hit: keyword=%r matched %d chunks",
                        keyword,
                        len(filtered["ids"][0]),
                    )
            except Exception as exc:
                logger.warning("hybrid filtered vector query failed (%s) — unfiltered fallback", exc)

        if vector_results is None:
            vector_results = collection.query(
                query_texts=[req.query],
                n_results=n_pull,
            )

        v_ids = vector_results.get("ids", [[]])[0]
        v_docs = vector_results.get("documents", [[]])[0]
        v_metas = vector_results.get("metadatas", [[]])[0]
        v_dists = vector_results.get("distances", [[]])[0]

        vector_hits: list[tuple[str, dict, str, float]] = list(
            zip(v_ids, [m or {} for m in v_metas], v_docs, v_dists)
        )

        # --- 2. BM25 search ---
        # Filter by canonical law name if detected (improves precision)
        bm25_raw = _bm25_search(
            req.query,
            collection,
            n_pull,
            law_name_filter=canonical_law,
        )
        # If law-filtered BM25 returns < 5 results, also run unfiltered and merge
        if canonical_law and len(bm25_raw) < 5:
            bm25_unfiltered = _bm25_search(req.query, collection, n_pull, law_name_filter=None)
            seen = {x[0] for x in bm25_raw}
            bm25_raw = bm25_raw + [x for x in bm25_unfiltered if x[0] not in seen]

        # --- 3. RRF fusion ---
        fused = _rrf_fuse(vector_hits, bm25_raw, top_k=_HYBRID_PULL_K)

        # --- 4. law_id boost ---
        fused = _law_id_boost(fused, canonical_law, boost=0.3)

        # --- 5. LLM rerank ---
        reranked = await _llm_rerank(req.query, fused, mode=req.mode, top_k=_TOP_K)

    except Exception as exc:
        logger.warning("ChromaDB pipeline failed (%s) — falling back to grep_search", exc)
        from services.data.legalize_kr import grep_search, GrepHit
        try:
            grep_result = await grep_search(
                req.query,
                limit=10,
                mode="OR" if req.laws else None,
                law_filter=req.laws or None,
            )
            def _grep_to_citation(hit: GrepHit) -> Citation:
                import re as _re
                excerpt_lines = [ln for ln in hit.excerpt.split("\n") if ln.strip()]
                body_lines = []
                for ln in excerpt_lines:
                    m = _re.match(r"^[^:]+[-:](\d+)[-:](.*)$", ln)
                    if m:
                        body_lines.append(m.group(2))
                    else:
                        body_lines.append(ln)
                excerpt = " \u00b7 ".join(line.strip() for line in body_lines if line.strip())[:400]
                return Citation(
                    law_id=hit.law_name,
                    law_name=hit.law_name,
                    article=hit.type or "\ubc95\ub839",
                    version="",
                    excerpt=excerpt,
                )
            if grep_result.error: logger.warning("grep_search error: %s", grep_result.error)
            grep_citations = [_grep_to_citation(h) for h in grep_result.hits]
            confidence = min(0.6 + 0.04 * len(grep_citations), 0.95) if grep_citations else 0.0
            if confidence >= 0.7:
                verdict = "applies"
            elif confidence >= 0.4:
                verdict = "ambiguous"
            else:
                verdict = "does_not_apply"
            return SearchResponse(
                verdict=verdict,
                confidence=round(confidence, 3),
                citations=grep_citations,
                trajectory_id=None,
                mode="fast",
            )
        except Exception as grep_exc:
            logger.error("grep_search fallback also failed: %s", grep_exc)
            return SearchResponse(
                verdict="ambiguous",
                confidence=0.0,
                citations=[],
                trajectory_id=None,
                mode=req.mode,
            )

    citations = [
        _meta_to_citation(doc, meta, score)
        for _, meta, doc, score in reranked[:_TOP_K]
    ]

    # Confidence from best RRF score (normalised: typical max RRF ~0.016 for rank 1 of 2 lists)
    confidence = 0.0
    if reranked:
        best_score = reranked[0][3]
        # RRF scores are small (~0.008–0.033); map to [0,1] with a practical ceiling
        confidence = round(min(1.0, best_score / 0.033), 3)

    if confidence >= 0.7:
        verdict = "applies"
    elif confidence >= 0.4:
        verdict = "ambiguous"
    else:
        verdict = "does_not_apply"

    return SearchResponse(
        verdict=verdict,
        confidence=confidence,
        citations=citations,
        trajectory_id=None,
        mode=req.mode,
    )
