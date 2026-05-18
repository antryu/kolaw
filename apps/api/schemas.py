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
    provenance: str = Field(
        "kolaw-index",
        description=(
            "수집 경위(provenance) — 신뢰도 추정이 아니라 인용이 어떻게 수집됐는지 기술. "
            "kolaw 검색 결과는 전부 인덱스 retrieval 이므로 기본값 'kolaw-index'."
        ),
    )
    verified_date: str = Field(
        "",
        description=(
            "해당 법령의 시행일/확인일 YYYYMMDD — version(시행일자)을 그대로 사용. "
            "한국법은 시행령·고시가 자주 바뀌어 시행일 명시가 중요. 미상 시 빈 문자열."
        ),
    )


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="검색 쿼리")
    mode: Literal["fast", "deep"] = Field(
        "deep",
        description=(
            "fast=hybrid BM25+vector + qwen3 rerank (local, ~5-20s); "
            "deep=hybrid BM25+vector + Opus 4.7 rerank (Anthropic API, ~10-25s). "
            "Both modes use the same hybrid retrieval pipeline. "
            "Use rlm=true for full RLM multi-step reasoning."
        ),
    )
    laws: list[str] | None = Field(None, description="특정 law_id 필터 (optional)")
    law_name: str | None = Field(
        None,
        description="법령명 기반 BM25 필터 — 예: '형사소송법'. 지정 시 해당 법령 문서를 우선 검색 (P1-2).",
    )
    rlm: bool = Field(False, description="Set true to use Phase 3 RLM engine for deep multi-step reasoning")


class SearchResponse(BaseModel):
    verdict: Literal["applies", "does_not_apply", "ambiguous"] | None = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    citations: list[Citation]
    trajectory_id: str | None = Field(
        None, description="non-null only for deep mode; used for Counsely Track C audit"
    )
    mode: Literal["fast", "deep"]
    error: str | None = Field(
        None,
        description="Error code if deep mode fails (e.g. 'local_llm_unavailable'). "
        "Caller must handle explicitly — no silent fallback.",
    )


class BatchSearchRequest(BaseModel):
    queries: list[SearchRequest] = Field(..., min_length=1)


class BatchSearchResponse(BaseModel):
    results: list[SearchResponse]


class ArticleResponse(BaseModel):
    """
    Verbatim text of one law article — deterministic file-parse lookup.

    Unlike /search (vector retrieval, document-level chunks), /article is a
    pure legalize-kr markdown parse: it returns the EXACT requested 제N조 text
    including its 항/호, with source-file provenance. No embeddings.
    """

    found: bool = Field(..., description="조문을 찾았으면 true")
    law_name: str = Field(..., description="법령명 (frontmatter 제목), e.g. '개인정보 보호법'")
    law_id: str = Field("", description="법령ID, e.g. '011357'")
    version: str = Field("", description="시행일자 YYYYMMDD, e.g. '20251002'")
    article: str = Field(..., description="조문 참조 (정규화), e.g. '제15조' / '제14조의2'")
    title: str = Field("", description="조문 제목, e.g. '(개인정보의 수집ㆍ이용)'")
    text: str = Field(
        "",
        description="조문 원문(verbatim) — 항(①②③)·호(1.2.3.) 포함, 다음 제N조 직전까지.",
    )
    type: str = Field("", description="법령 종류 — 법률 / 시행령 / 시행규칙 / ...")
    source_path: str = Field(
        "",
        description="원문 마크다운 파일 절대경로 — provenance(수집 경위).",
    )
    provenance: str = Field(
        "legalize-kr-file",
        description="수집 경위 — /article 은 항상 legalize-kr 파일 직접 파싱.",
    )
    error: str | None = Field(
        None, description="조문/법령 미발견 시 사람이 읽을 수 있는 사유."
    )


class DataSourceStatus(BaseModel):
    name: str
    status: Literal["ok", "degraded", "unavailable"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    data_sources: list[DataSourceStatus]
