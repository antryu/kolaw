# CLAUDE.md — kolaw

## Identity

**Korean Law Library & Research Infra** — serves y-Tower agents (primarily Legaly, 9F).
Internal infrastructure. Not an agent. Not an external law firm.
Private repo: github.com/antryu/kolaw

## 3-Source Architecture

```
                    ┌─────────────────────────────────────────┐
                    │           antryu/kolaw                  │
                    │                                         │
  Agent query ──►  │  /search (fast)  ──►  ChromaDB          │
                    │                       + legalize-kr     │
                    │  /search (deep)  ──►  RLM Engine        │
                    │                       + Qwen3-32B local │
                    └─────────────────┬───────────────────────┘
                                      │
                    ┌─────────────────▼───────────────────────┐
                    │          Data Sources (3)               │
                    │                                         │
                    │  1. legalize-kr (9bow/legalize-kr)      │
                    │     2303 statutes, Markdown, local      │
                    │                                         │
                    │  2. korean-law-mcp (chrisryugj)         │
                    │     64 MCP tools: 법령+판례+행정규칙    │
                    │     +조례+헌재+조세심판+관세            │
                    │                                         │
                    │  3. beopmang (api.beopmang.org)         │
                    │     Structured metadata:                │
                    │     article_count, case_count,          │
                    │     xref_count, history_count           │
                    └─────────────────────────────────────────┘
```

## LLM Routing

**anthropic_approval_gate** applies: automation defaults to local M4 llama.cpp.
Anthropic paid API only with explicit Andrew approval (ALLOW_ANTHROPIC=1).

```
Primary:  http://127.0.0.1:8080/v1  (llama-swap, Qwen3-32B)
Fallback: ANTHROPIC_API_KEY         (gated: ALLOW_ANTHROPIC=1 required)
```

## API — JSON Schema Examples

### POST /search (fast mode)

Request:
```json
{
  "query": "수소충전소 허가 요건",
  "mode": "fast",
  "laws": ["013670"]
}
```

Response:
```json
{
  "verdict": "applies",
  "confidence": 0.87,
  "citations": [
    {
      "law_id": "013670",
      "law_name": "수소경제 육성 및 수소 안전관리에 관한 법률",
      "article": "§44",
      "version": "20251001",
      "excerpt": "수소연료공급시설을 설치·운영하려는 자는 산업통상자원부장관..."
    }
  ],
  "trajectory_id": null,
  "mode": "fast"
}
```

### POST /search (deep mode)

Response adds `trajectory_id` for Counsely Track C audit:
```json
{
  "verdict": "ambiguous",
  "confidence": 0.5,
  "citations": [...],
  "trajectory_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "mode": "deep"
}
```

## Commands

```bash
# Local dev
pip install -e .
uvicorn apps.api.main:app --reload --port 8100

# Tests
pytest

# Ingest fixture data
python -m services.fast_search.ingest

# Docker
docker-compose up
curl http://localhost:8100/health
```

## RLM Reference

arXiv 2512.24601v2 (Recursive Language Models).
Phase 1: REPL stub with exec() sandbox.
Phase 2: harden with RestrictedPython or Docker isolation.

## Phase Status

- Phase 1 (current): scaffold, stubs, fixture data, 4 tests passing
- Phase 2 (pending Andrew approval): RLM multi-turn, full legalize-kr index,
  korean-law-mcp wire, beopmang enrichment, sandbox hardening

## 관할 가정 표면화

**원칙**: kolaw 의 default 준거법은 **대한민국 실정법**이다. 외국법이 교차 적용될 가능성을 감지하면 반드시 표면화하라.

**의무 태그 부착 상황**:

| 상황 | 부착 태그 |
|---|---|
| 국제 계약 / 준거법 조항 개입 | `[한국법 기준 — 준거법 확인 필요]` |
| GDPR · 미국 수출규제 등 외국법 교차 적용 가능성 | `[한국법 기준 — 준거법 확인 필요]` |
| 미국·EU 판례를 국내법 해석에 원용 | `[참고 — 한국법과 다를 수 있음, 외국법 전문가 확인]` |
| 단일 국내 법령 질의 (명백) | 태그 불필요 |

**배경**: claude-for-legal 가드레일 — "wrong jurisdiction 으로 자신있게 틀리느니 불확실을 플래그하라."
y-company `runCell` CELL_SYSTEM_PROMPT RULE 8 과 동기화됨.
