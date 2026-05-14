"""
kolaw FastAPI — Korean Law Library & Research Infra
Agent-facing HTTP API. Port 8100.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from apps.api.schemas import (
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
