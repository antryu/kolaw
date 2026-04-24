# kolaw Architecture

## Overview

RLM-based Korean legal research engine for y-Tower agents.

Reference: arXiv 2512.24601v2 (Recursive Language Models)
Design difference from hydrogen-law: general-purpose + RLM-inclusive (not LLM-minimal).

## Component Map

```
apps/api/
├── main.py          FastAPI app, 3 endpoints
└── schemas.py       Pydantic v2 response models

services/
├── fast_search/
│   ├── ingest.py    Fixture ingest into ChromaDB
│   └── search.py    Vector query → Citation list
├── rlm_engine/
│   ├── repl.py      RLMSession: load/exec/get
│   └── orchestrator.py  Query → LLM code → exec → FINAL_ANSWER
├── data/
│   ├── legalize_kr.py   Markdown corpus loader
│   ├── kolmcp_client.py  korean-law-mcp stub
│   └── beopmang_client.py  법망 API client
└── llm/
    └── router.py    Local Qwen3 primary + Claude fallback
```

## Search Paths

### Fast Path (ChromaDB)

```
POST /search?mode=fast
  → services/fast_search/search.py
  → ChromaDB.query(query_texts=[query], n_results=5)
  → rank by distance → build Citations
  → SearchResponse (trajectory_id=None)
```

### Deep Path (RLM Engine)

```
POST /search?mode=deep
  → services/rlm_engine/orchestrator.py
  → build system prompt
  → services/llm/router.py → complete()
    → local: http://127.0.0.1:8080/v1/chat/completions
    → fallback (ALLOW_ANTHROPIC=1): Claude Sonnet
  → RLMSession.exec(generated_code)
  → RLMSession.get("FINAL_ANSWER")
  → SearchResponse (trajectory_id=uuid)
```

## Data Flow — Ingest

```
~/PRJs/hydrogen-law/services/rag-engine/law_documents.json
  → services/fast_search/ingest.py
  → jhgan/ko-sroberta-multitask embeddings
  → ChromaDB collection "kolaw_laws"
```

Phase 1: 5-document fixture from hydrogen-law JSON.
Phase 2: full legalize-kr corpus (2303 statutes).

## Separation from hydrogen-law

`antryu1b/hydrogen-law` stays as vertical commercial product (수소법 only, LLM-minimal).
`antryu/kolaw` is internal, general-purpose, agent-facing, RLM-inclusive.
Code is copied with attribution, not shared via import.
