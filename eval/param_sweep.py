"""
In-process parameter sweep for law_id_boost and RRF k.
Patches search.py module variables before each run.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.fast_search.search as search_mod
from apps.api.schemas import SearchRequest

QUERIES = [
    "의료법 진료기록 보존기간",
    "의료법 시행규칙 처방전 보관기간",
    "의료광고 의료법 위반",
    "근로기준법 연차휴가",
    "근로기준법 시간외근로",
    "근로기준법 임금체불 시효",
    "민법 소멸시효 5년",
    "민법 보증인 책임",
    "민법 채무인수 효력",
    "형법 정당방위 요건",  # corpus_gap — will always miss
]

EXPECTED_KEYWORDS: dict[str, list[str]] = {
    "의료법 진료기록 보존기간": ["의료법", "진료기록", "보존"],
    "의료법 시행규칙 처방전 보관기간": ["의료법", "처방전", "보관"],
    "의료광고 의료법 위반": ["의료법", "의료광고"],
    "근로기준법 연차휴가": ["근로기준법", "연차"],
    "근로기준법 시간외근로": ["근로기준법", "시간외"],
    "근로기준법 임금체불 시효": ["근로기준법", "임금"],
    "민법 소멸시효 5년": ["민법", "소멸시효"],
    "민법 보증인 책임": ["민법", "보증"],
    "민법 채무인수 효력": ["민법", "채무인수"],
    "형법 정당방위 요건": ["형법", "정당방위"],
}


def _is_hit(query: str, citations) -> bool:
    if not citations:
        return False
    c = citations[0]
    text = " ".join([c.law_name or "", c.article or "", c.excerpt or ""])
    return any(kw in text for kw in EXPECTED_KEYWORDS.get(query, []))


def _is_hit_at_3(query: str, citations) -> bool:
    return any(
        any(kw in " ".join([c.law_name or "", c.article or "", c.excerpt or ""])
            for kw in EXPECTED_KEYWORDS.get(query, []))
        for c in (citations[:3] if citations else [])
    )


async def eval_params(mode: str = "fast", quiet: bool = False) -> tuple[int, int]:
    """Run all queries and return (hit_at_1, hit_at_3)."""
    h1 = 0
    h3 = 0
    for q in QUERIES:
        req = SearchRequest(query=q, mode=mode)
        resp = await search_mod.fast_search(req)
        if _is_hit(q, resp.citations):
            h1 += 1
        if _is_hit_at_3(q, resp.citations):
            h3 += 1
    return h1, h3


async def sweep_boost():
    """Sweep law_id boost values {0.2, 0.3, 0.4, 0.5} using fast mode."""
    print("\n=== law_id boost sweep (fast mode) ===")
    print(f"{'boost':>6} | {'hit@1':>5} | {'hit@3':>5}")
    print("-" * 25)
    best = (0.3, 0, 0)
    for boost in [0.2, 0.3, 0.4, 0.5]:
        # Invalidate BM25 cache so module re-reads fresh state
        search_mod._BM25_CACHE.clear()
        # Monkey-patch the boost default in _law_id_boost
        import functools
        orig_boost_fn = search_mod._law_id_boost

        _b = boost  # capture current loop value

        def patched_boost(hits, canonical, **_kw):
            return orig_boost_fn(hits, canonical, boost=_b)

        search_mod._law_id_boost = patched_boost
        h1, h3 = await eval_params(mode="fast")
        print(f"{boost:>6.1f} | {h1:>5} | {h3:>5}")
        if h1 > best[1] or (h1 == best[1] and h3 > best[2]):
            best = (boost, h1, h3)
        # Restore
        search_mod._law_id_boost = orig_boost_fn

    print(f"\nBest boost: {best[0]} (hit@1={best[1]}, hit@3={best[2]})")
    return best[0]


async def sweep_rrf():
    """Sweep RRF k values {30, 60, 100} using fast mode."""
    print("\n=== RRF k sweep (fast mode) ===")
    print(f"{'k':>6} | {'hit@1':>5} | {'hit@3':>5}")
    print("-" * 25)
    best = (60, 0, 0)
    for k in [30, 60, 100]:
        search_mod._BM25_CACHE.clear()
        # Patch _rrf_fuse default k
        orig_fuse = search_mod._rrf_fuse
        _k = k  # capture current loop value

        def patched_fuse(vh, bh, top_k, **_kw):
            return orig_fuse(vh, bh, top_k=top_k, k=_k)

        search_mod._rrf_fuse = patched_fuse
        h1, h3 = await eval_params(mode="fast")
        print(f"{k:>6} | {h1:>5} | {h3:>5}")
        if h3 > best[2] or (h3 == best[2] and h1 > best[1]):
            best = (k, h1, h3)
        search_mod._rrf_fuse = orig_fuse

    print(f"\nBest RRF k: {best[0]} (hit@1={best[1]}, hit@3={best[2]})")
    return best[0]


async def sweep_topk():
    """Sweep fast-mode rerank candidate count {5, 10} — measures latency impact."""
    import time
    print("\n=== fast-mode rerank top-K sweep ===")
    print(f"{'topk':>6} | {'hit@1':>5} | {'hit@3':>5} | {'avg_lat_ms':>12}")
    print("-" * 40)
    best_topk = 10
    for topk in [5, 10]:
        orig_rerank = search_mod._llm_rerank

        async def patched_rerank(q, hits, mode, top_k, tk=topk):
            # Override to use tk as the candidate count
            candidates = hits[:tk]
            # Call original with patched candidate list
            return await orig_rerank(q, candidates, mode, top_k)

        search_mod._llm_rerank = patched_rerank
        h1 = 0
        h3 = 0
        lats = []
        for q in QUERIES:
            req = SearchRequest(query=q, mode="fast")
            t0 = time.time()
            resp = await search_mod.fast_search(req)
            lats.append(int((time.time() - t0) * 1000))
            if _is_hit(q, resp.citations):
                h1 += 1
            if _is_hit_at_3(q, resp.citations):
                h3 += 1
        avg_lat = sum(lats) / len(lats)
        print(f"{topk:>6} | {h1:>5} | {h3:>5} | {avg_lat:>12.0f}")
        if h1 >= 9 and topk < best_topk:
            best_topk = topk
        search_mod._llm_rerank = orig_rerank

    print(f"\nBest top-K: {best_topk}")
    return best_topk


async def main():
    # Prime the BM25 index (one-time build)
    print("Priming BM25 index...")
    _ = search_mod._get_collection()

    best_boost = await sweep_boost()
    best_k = await sweep_rrf()
    best_topk = await sweep_topk()

    print("\n=== Recommended params ===")
    print(f"  law_id_boost = {best_boost}")
    print(f"  RRF k        = {best_k}")
    print(f"  fast top-K   = {best_topk}")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
