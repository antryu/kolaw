"""
Response schemas for kolaw API.

Structured citations with trajectory audit support for Counsely Track C.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Citation(BaseModel):
    law_id: str = Field(..., description="법령일련번호, e.g. '013670'")
    law_name: str = Field(..., description="법령명, e.g. '수소경제 육성 및 수소 안전관리에 관한 법률'")
    article: str = Field(..., description="조문 참조, e.g. '§2(7)'")
    version: str = Field(..., description="시행일자 YYYYMMDD, e.g. '20251001'")
    excerpt: str = Field(..., description="원문 발췌 (max 200 chars)")


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="검색 쿼리")
    mode: Literal["fast", "deep"] = Field("fast", description="fast=ChromaDB BM25, deep=RLM engine")
    laws: list[str] | None = Field(None, description="특정 law_id 필터 (optional)")


class SearchResponse(BaseModel):
    verdict: Literal["applies", "does_not_apply", "ambiguous"] | None = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    citations: list[Citation]
    trajectory_id: str | None = Field(
        None, description="non-null only for deep mode; used for Counsely Track C audit"
    )
    mode: Literal["fast", "deep"]


class BatchSearchRequest(BaseModel):
    queries: list[SearchRequest] = Field(..., min_length=1)


class BatchSearchResponse(BaseModel):
    results: list[SearchResponse]


class DataSourceStatus(BaseModel):
    name: str
    status: Literal["ok", "degraded", "unavailable"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    data_sources: list[DataSourceStatus]
