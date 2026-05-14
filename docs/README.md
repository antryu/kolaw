# kolaw — Korean Law Library & Research Infra

Internal infrastructure for y-Tower agents. Not an external product.
Primary consumer: **Legaly** (9F legal research agent).

## Architecture

3 data sources → 2 search paths → 1 agent-facing API.

| Source | Type | Phase |
|--------|------|-------|
| `github.com/9bow/legalize-kr` | 2303 statutes as Markdown | Phase 1 (local) |
| `github.com/chrisryugj/korean-law-mcp` | 64 MCP tools | Phase 2 (wire) |
| `api.beopmang.org` | Structured metadata | Phase 1 (client) |

**Fast path** (90%): ChromaDB vector search on indexed corpus.
**Deep path** (10%): RLM Engine — LLM writes search code, REPL executes, captures FINAL_ANSWER.

## API

Port 8100. See `docs/API.md` for full spec.

```bash
GET  /health          # service status + data source availability
POST /search          # {query, mode: "fast"|"deep", laws?: [...]}
POST /search/batch    # [{query, mode}, ...] → [{...}]
```

## Quick Start

```bash
# Clone
git clone https://github.com/antryu/kolaw

# Install
pip install -e .

# Run
uvicorn apps.api.main:app --port 8100

# Docker (recommended for agents)
docker-compose up
curl http://localhost:8100/health
```

## LLM Policy

Local Qwen3-32B (llama-swap, port 8080) is primary.
Claude Sonnet fallback requires `ALLOW_ANTHROPIC=1` (explicit Andrew approval).
See `anthropic_approval_gate` memory.
