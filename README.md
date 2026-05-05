# kolaw — Korean Law Aggregator

> **Open-source aggregator for Korean law data sources.**
> One API to search 한국 법령·판례·해석·헌재결정, combining offline statute corpus with the best-in-class MCP servers.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.124+-009688.svg)](https://fastapi.tiangolo.com)

## Why kolaw?

Korean law tooling has matured rapidly — multiple excellent MCP servers exist
([korean-law-mcp][chrisryugj], [LexGuard][lexguard]) — each with its own strengths.
**kolaw doesn't replace them. It unifies them.**

- **Offline statute corpus** via [legalize-kr][legalize-kr] (MIT) — 2,303 laws as markdown,
  works without an API key, with full **revision history via `git log`** ("what did this
  law say in 2020?").
- **Live case law & interpretations** via the Korean MCP ecosystem
  (chrisryugj, LexGuard) — real-time 판례·해석·헌재결정.
- **Vector search (ChromaDB)** + **LLM-driven autonomous search (RLM)** for queries that
  pure keyword search can't handle.
- **One JSON API** (`POST /search`) abstracts all of it. Switch sources with config.

## Architecture

```
                ┌──────────────────────────────────┐
                │         antryu/kolaw              │
                │      FastAPI on :8100             │
                └──────────────────────────────────┘
                                │
       ┌────────────────────────┼────────────────────────┐
       ▼                        ▼                        ▼
┌──────────────┐       ┌─────────────────┐     ┌────────────────────┐
│ legalize-kr  │       │ korean-law-mcp  │     │ lexguard-mcp       │
│ 2,303 laws   │       │ chrisryugj      │     │ SeoNaRu            │
│ MIT, offline │       │ 16 tools / 41   │     │ 18 tools / 159     │
│ git history  │       │ APIs, citation  │     │ APIs, reranker     │
│              │       │ verification    │     │ + domain classify  │
└──────────────┘       └─────────────────┘     └────────────────────┘
       │                        │                        │
       └────────────────────────┴────────────────────────┘
                                │
                ┌───────────────┴───────────────┐
                │  ChromaDB (vector search)     │
                │  RLM Engine (LLM autonomous)  │
                └───────────────────────────────┘
```

| Source | Type | License | Status |
|--------|------|---------|--------|
| [9bow/legalize-kr][legalize-kr] | 2,303 statutes (Markdown + git) | MIT | Phase 1 ✅ |
| [api.beopmang.org][beopmang] | Structured metadata (article/case counts) | — | Phase 1 ✅ |
| [chrisryugj/korean-law-mcp][chrisryugj] | 16 MCP tools / 41 법제처 APIs | — | Phase 2 🔧 |
| [SeoNaRu/lexguard-mcp][lexguard] | 18 MCP tools / 159 APIs | MIT | Phase 3 📋 |

**Two search paths:**
- **Fast** (~90% of queries): ChromaDB vector search over indexed corpus + direct grep
- **Deep** (~10%): RLM Engine — LLM writes search code, REPL executes, captures `FINAL_ANSWER`

## Quick Start

```bash
git clone https://github.com/antryu/kolaw
cd kolaw
docker compose up --build
curl http://localhost:8100/health
```

Or local Python:
```bash
pip install -e .
uvicorn apps.api.main:app --port 8100
```

### Search

```bash
# Fast: keyword + vector search
curl -X POST http://localhost:8100/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"수소충전소 허가 요건","mode":"fast"}'

# Deep: LLM-driven autonomous search
curl -X POST http://localhost:8100/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"근로자성 인정 판례 최근 3년","mode":"deep"}'
```

Response shape:
```json
{
  "verdict": "applies",
  "confidence": 0.87,
  "citations": [{
    "law_id": "013670",
    "law_name": "수소경제 육성 및 수소 안전관리에 관한 법률",
    "article": "§44",
    "version": "20251001",
    "excerpt": "..."
  }],
  "trajectory_id": null,
  "mode": "fast"
}
```

See [`docs/API.md`](docs/API.md) for the full specification.

## Configuration

Copy `.env.example` to `.env` and set values:

```bash
# LLM (primary: local Qwen3-32B via llama.cpp / llama-swap)
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1
LOCAL_LLM_MODEL=qwen3:32b

# Optional Anthropic fallback (gated)
ALLOW_ANTHROPIC=0

# Data sources
LEGALIZE_KR_PATH=/data/legalize-kr   # mount your local clone of 9bow/legalize-kr
BEOPMANG_BASE_URL=https://api.beopmang.org/api/v4

# ChromaDB
CHROMA_HOST=chromadb
CHROMA_PORT=8000
```

## Roadmap

- [x] **Phase 1** — legalize-kr local search, beopmang client, FastAPI scaffold, RLM stub
- [ ] **Phase 2** — chrisryugj/korean-law-mcp integration (판례·해석)
- [ ] **Phase 3** — LexGuard MCP integration (159 APIs, reranker, contract analysis)
- [ ] **Phase 4** — Full ChromaDB index of 2,303 statutes (sentence-transformers)
- [ ] **Phase 5** — RLM Engine production hardening (RestrictedPython / Docker sandbox)
- [ ] **Phase 6** — MCP server wrapper (so kolaw itself can be consumed via MCP)

## Contributing

Pull requests welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup,
test policy, and design conventions.

## Credits

kolaw stands on the shoulders of others:
- [9bow/legalize-kr][legalize-kr] — the offline statute corpus that makes Phase 1 possible
- [chrisryugj/korean-law-mcp][chrisryugj] — citation-verified MCP for 법제처 APIs
- [SeoNaRu/lexguard-mcp][lexguard] — the most comprehensive Korean law MCP (159 APIs, reranker)
- [api.beopmang.org][beopmang] — structured metadata for cross-referencing

Without these, kolaw would have to re-implement decades of work. Thank you.

## License

MIT — see [LICENSE](LICENSE).

[legalize-kr]: https://github.com/9bow/legalize-kr
[chrisryugj]: https://github.com/chrisryugj/korean-law-mcp
[lexguard]: https://github.com/SeoNaRu/lexguard-mcp
[beopmang]: https://api.beopmang.org
