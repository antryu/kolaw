"""
kolaw FastAPI — Korean Law Library & Research Infra
Agent-facing HTTP API. Port 8100.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware

from apps.api.schemas import (
    ArticleResponse,
    BatchSearchRequest,
    BatchSearchResponse,
    DataSourceStatus,
    HealthResponse,
    SearchRequest,
    SearchResponse,
)
from services.fast_search.search import fast_search
from services.rlm_engine.orchestrator import deep_search, deep_search_mock

VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 2 — warmup BM25 + ChromaDB to avoid cold-start timeout
    import logging
    _logger = logging.getLogger(__name__)
    try:
        from services.fast_search.search import _get_collection, _get_bm25_index
        _col = _get_collection()
        _get_bm25_index(_col)
        _logger.info("kolaw startup: BM25 + collection warmed")
    except Exception as _e:
        _logger.warning("kolaw warmup failed: %s", _e)
    yield
    # Shutdown


app = FastAPI(
    title="kolaw",
    description="Korean Law Library & Research Infra for y-Tower agents",
    version=VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
async def health():
    """Returns service status and data source availability."""
    sources: list[DataSourceStatus] = []

    # legalize-kr: check if loader can list files
    try:
        from services.data.legalize_kr import list_available_laws

        count = len(list_available_laws())
        sources.append(
            DataSourceStatus(
                name="legalize-kr",
                status="ok",
                detail=f"{count} laws available",
            )
        )
    except Exception as exc:
        sources.append(
            DataSourceStatus(name="legalize-kr", status="degraded", detail=str(exc))
        )

    # beopmang: stub check (Phase 1 — no live call in health)
    sources.append(
        DataSourceStatus(
            name="beopmang",
            status="ok",
            detail="client wired; Phase 1 stub",
        )
    )

    # korean-law-mcp: stub
    sources.append(
        DataSourceStatus(
            name="korean-law-mcp",
            status="ok",
            detail="64 tools documented; Phase 1 stub — no live MCP call",
        )
    )

    overall = (
        "ok"
        if all(s.status == "ok" for s in sources)
        else "degraded"
    )
    return HealthResponse(status=overall, version=VERSION, data_sources=sources)


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, response: Response) -> SearchResponse:
    """
    Law search endpoint — Y option hybrid retrieval.

    mode=fast:  BM25 + vector hybrid + qwen3 rerank (local llama-swap, ~5-20s)
    mode=deep:  BM25 + vector hybrid + Claude Opus 4.7 rerank (~10-25s, ALLOW_ANTHROPIC required)

    Both modes use the same hybrid retrieval pipeline (BM25 + ChromaDB + RRF + law_id boost).
    The `mode` param selects only the LLM reranker. Default is deep.

    rlm=true:   Phase 3 RLM multi-step reasoning engine (requires local LLM, ~30-120s).
                Uses same hybrid retrieval as prefilter, then orchestrates sub-LLM calls.
    """
    if req.rlm:
        return await deep_search(req)
    # Both fast and deep use hybrid retrieval; mode drives the reranker LLM.
    return await fast_search(req)


@app.get("/article", response_model=ArticleResponse)
async def article(
    response: Response,
    law: str = Query(..., min_length=1, description="법령명, e.g. '개인정보보호법'"),
    article: str = Query(
        ..., min_length=1, alias="article",
        description="조문 참조, e.g. '제15조' 또는 '제14조의2'",
    ),
    type: str = Query(
        "법률",
        description="법령 종류 — 법률(기본) / 시행령 / 시행규칙 / 대통령령 / 대법원규칙",
    ),
) -> ArticleResponse:
    """
    Deterministic per-article lookup — returns one article's verbatim text.

    /search is vector retrieval over document-level chunks and cannot reliably
    surface a *specific* article's exact text. /article is a pure file parse:
    it locates the law's folder under the legalize-kr corpus, opens the
    requested markdown file, splits on 제N조 headings (reusing the existing
    legalize_kr splitter), and returns the EXACT requested article — its 항/호
    included — up to the next 제N조 heading, with source-file provenance.

    No embeddings, no vector search. Use /search for "which law is relevant",
    /article for "give me 제N조 verbatim".

    Returns found=false + error (HTTP 404) when the law or article is missing.
    """
    from services.data.article_lookup import lookup_article

    result = lookup_article(law_name=law, article_ref=article, law_type=type)
    if not result.found:
        response.status_code = 404

    # Phase 2: attach the delegation chain this article belongs to, if indexed.
    delegation_chain = None
    if result.found:
        try:
            from apps.api.schemas import DelegationChain
            from services.crossref.lookup import get_delegation_chain_by_article

            chain_dict = get_delegation_chain_by_article(
                law_id=result.law_id,
                file_type=result.type,
                article=result.article,
            )
            if chain_dict is not None:
                delegation_chain = DelegationChain(**chain_dict)
        except Exception:  # crossref lookup must never break /article
            delegation_chain = None

    return ArticleResponse(
        found=result.found,
        law_name=result.law_name,
        law_id=result.law_id,
        version=result.version,
        article=result.article,
        title=result.title,
        text=result.text,
        type=result.type,
        source_path=result.source_path,
        provenance="legalize-kr-file",
        delegation_chain=delegation_chain,
        error=result.error,
    )


@app.post("/search/batch", response_model=BatchSearchResponse)
async def search_batch(req: BatchSearchRequest) -> BatchSearchResponse:
    """
    Batch search. Phase 1: sequential. Phase 2 will parallelize.
    """
    results = []
    for query in req.queries:
        if query.rlm:
            result = await deep_search(query)
        else:
            result = await fast_search(query)
        results.append(result)
    return BatchSearchResponse(results=results)
