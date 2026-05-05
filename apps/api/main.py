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
    # Startup: nothing heavy in Phase 1
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

    # law.go.kr direct API (Phase 3)
    try:
        from services.data.law_go_kr import LawGoKrClient

        ok, msg = await LawGoKrClient().is_available()
        sources.append(
            DataSourceStatus(
                name="law.go.kr",
                status="ok" if ok else "degraded",
                detail=msg,
            )
        )
    except Exception as exc:
        sources.append(
            DataSourceStatus(name="law.go.kr", status="degraded", detail=str(exc))
        )

    # data.go.kr 헌법재판소 판례 (separate auto-approved API; covers detc target
    # without needing law.go.kr's manual permission flow)
    try:
        from services.data.data_go_kr_court import DataGoKrCourtClient

        ok, msg = await DataGoKrCourtClient().is_available()
        sources.append(
            DataSourceStatus(
                name="data.go.kr-헌재",
                status="ok" if ok else "degraded",
                detail=msg,
            )
        )
    except Exception as exc:
        sources.append(
            DataSourceStatus(name="data.go.kr-헌재", status="degraded", detail=str(exc))
        )

    # LexGuard MCP (Phase 4) — optional reranker / domain classifier
    try:
        from services.data.lexguard_client import LexGuardClient

        ok, msg = await LexGuardClient().is_available()
        sources.append(
            DataSourceStatus(
                name="lexguard-mcp",
                status="ok" if ok else "degraded",
                detail=msg,
            )
        )
    except Exception as exc:
        sources.append(
            DataSourceStatus(name="lexguard-mcp", status="degraded", detail=str(exc))
        )

    # beopmang: stub (Phase 1 — supplementary metadata)
    sources.append(
        DataSourceStatus(
            name="beopmang",
            status="ok",
            detail="client wired; supplementary metadata source",
        )
    )

    # korean-law-mcp (chrisryugj) — optional self-hosted MCP. Probe live.
    try:
        from services.data.kolmcp_client import KolMCPClient

        ok, msg = await KolMCPClient().is_available()
        sources.append(
            DataSourceStatus(
                name="korean-law-mcp",
                status="ok" if ok else "degraded",
                detail=msg,
            )
        )
    except Exception as exc:
        sources.append(
            DataSourceStatus(name="korean-law-mcp", status="degraded", detail=str(exc))
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
    Law search endpoint.
    mode=fast: ChromaDB vector search.
    mode=deep: RLM engine.
      Production: uses deep_search() (real minimal RLM loop, requires local LLM).
      Compat: uses deep_search_mock() stub for Phase 1 backward compat.
      Switch to deep_search when local LLM (llama-swap) is running.
      On local LLM unavailable: returns 503 with error='local_llm_unavailable'.
    """
    if req.mode == "fast":
        return await fast_search(req)
    else:
        # Phase 2: use mock stub for backward compat; switch to deep_search when LLM ready.
        # deep_search() is implemented and tested separately via test_rlm_minimal_loop.py.
        return await deep_search(req)


@app.post("/search/batch", response_model=BatchSearchResponse)
async def search_batch(req: BatchSearchRequest) -> BatchSearchResponse:
    """
    Batch search. Phase 1: sequential. Phase 2 will parallelize.
    """
    results = []
    for query in req.queries:
        if query.mode == "fast":
            result = await fast_search(query)
        else:
            result = await deep_search(query)
        results.append(result)
    return BatchSearchResponse(results=results)
