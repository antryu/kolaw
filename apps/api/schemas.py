"""
Response schemas for kolaw API.

Structured citations with trajectory audit support for Counsely Track C.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DelegationChain(BaseModel):
    """
    위임 체인 — 본법 조문이 시행령·시행규칙·별표로 위임한 관계 (Phase 2).

    services/crossref/index/<법령>.json 의 한 delegation_chain 을 그대로 옮긴
    구조. 검색·조문 조회 결과(Citation/ArticleResponse)에 부착되어, hit 된
    조문이 위임 체인의 어디에 있고 위·아래로 무엇이 연결되는지 보여준다.

    decree_articles / rule_articles / byeolpyo 는 색인 JSON 형식(각 항목이
    doc_id·article·title 등을 담은 dict)을 그대로 노출 — 별표·하위규칙 구조
    변동에 견디도록 느슨하게 둔다.
    """

    law_name: str = Field(..., description="이 체인이 속한 법령명, e.g. '개인정보 보호법'")
    law_id: str = Field("", description="법령ID, e.g. '011357'")
    law_article: str = Field(
        ..., description="위임의 출발점인 본법 조문(정규화), e.g. '제28조의8'"
    )
    law_doc_id: str = Field(
        "", description="본법 조문의 ChromaDB doc_id, e.g. '011357_개인정보보호법_법률_제28조_8'"
    )
    law_title: str = Field("", description="본법 조문 제목, e.g. '(개인정보의 국외 이전)'")
    delegation_kind: list[str] = Field(
        default_factory=list,
        description="위임 종류 — '대통령령' / '총리령·부령' 등",
    )
    byeolpyo: list[Any] = Field(
        default_factory=list,
        description=(
            "연결된 별표 — Phase 3 A안 enrich 형식. 각 항목 "
            "{별표: 번호, body_available: bool, bodies: [{별표명, 관련법령명, "
            "별표일련번호, is_image, body_available, text, tables:[{page, "
            "markdown}], pdf_url, ...}]}. bodies 는 1:N (같은 번호가 시행령· "
            "시행규칙으로 갈릴 수 있음). is_image=true 별표는 본문 대신 "
            "image 플래그만. 본문 사이드카가 없으면 bodies 빈 리스트."
        ),
    )
    decree_articles: list[dict] = Field(
        default_factory=list,
        description="위임된 시행령 조문들 — 각 항목 {doc_id, article, title, file_type}.",
    )
    rule_articles: list[dict] = Field(
        default_factory=list,
        description="위임된 시행규칙 조문들 — 각 항목 {doc_id, article, title, file_type}.",
    )
    tree_text: str = Field(
        "",
        description=(
            "위임 체인을 들여쓰기 계층으로 렌더한 순수 텍스트 트리 (Phase 3). "
            "본법→시행령→시행규칙→별표 구조를 한눈에 보여주며, hit 된 조문은 "
            "'▶' 마커로 표시. 평평한 링크 목록 대신 이 텍스트를 그대로 출력하면 된다."
        ),
    )


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
    delegation_chain: DelegationChain | None = Field(
        None,
        description=(
            "이 조문이 속한 위임 체인 (Phase 2). 본법↔시행령↔시행규칙↔별표 위임 "
            "관계가 crossref 색인에 있으면 부착, 없으면 None. None 이면 위임 관계가 "
            "색인되지 않은 조문 — 기존 동작과 동일하게 무시하면 된다."
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
    delegation_chain: DelegationChain | None = Field(
        None,
        description=(
            "이 조문이 속한 위임 체인 (Phase 2). 본법↔시행령↔시행규칙↔별표 위임 "
            "관계가 crossref 색인에 있으면 부착, 없으면 None."
        ),
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
