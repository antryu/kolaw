"""
kolaw FastAPI — Korean Law Library & Research Infra
Agent-facing HTTP API. Port 8100.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
from services.rlm_engine.orchestrator import deep_search_mock

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
async def search(req: SearchRequest) -> SearchResponse:
    """
    Law search endpoint.
    mode=fast: ChromaDB vector search on fixture data (Phase 1: 5 hydrogen law docs).
    mode=deep: RLM engine stub — returns mock trajectory_id.
    """
    if req.mode == "fast":
        return await fast_search(req)
    else:
        return await deep_search_mock(req)


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
            result = await deep_search_mock(query)
        results.append(result)
    return BatchSearchResponse(results=results)
