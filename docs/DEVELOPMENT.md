# Development Notes

Internal architecture and engineering decisions for kolaw maintainers.
For user-facing docs see the root [`README.md`](../README.md) and
[`docs/API.md`](API.md).

## Service layout

```
apps/api/         FastAPI app, Pydantic schemas
services/
  ├─ data/        Source adapters (one file per data source)
  │   ├─ legalize_kr.py    git grep over 9bow/legalize-kr (offline)
  │   ├─ beopmang_client.py  api.beopmang.org HTTP client
  │   └─ kolmcp_client.py    chrisryugj/korean-law-mcp HTTP client
  ├─ fast_search/ ChromaDB + keyword + reranker (Fast path)
  ├─ rlm_engine/  Recursive Language Model loop (Deep path)
  └─ llm/         LLM router — local Qwen first, optional Anthropic gated
tests/
docs/
```

Each `data/*.py` exposes a uniform shape so the API layer can fan out and merge:
```python
async def search(query: str, ...) -> list[Citation]
```

## Search paths

- **Fast** (~90% of queries): keyword + ChromaDB vector + reranker.
  Target latency: <500ms p95.
- **Deep** (~10%): RLM Engine — LLM is given REPL access to source adapters
  and writes its own search code. Returns when it emits `FINAL_ANSWER`.
  Target latency: <30s p95. Always returns a `trajectory_id` for audit.

Mode selection is the **caller's responsibility** (`mode: "fast" | "deep"`).
We don't auto-escalate yet — it would hide cost from the caller.

## LLM routing policy

```
Primary:  http://127.0.0.1:8080/v1  (llama-swap → Qwen3-32B Q4_K_M)
Fallback: Anthropic Claude (gated)
```

The fallback path requires `ALLOW_ANTHROPIC=1`. This is intentional — we don't
want a runaway Deep search to unexpectedly bill against a paid API. Set the
flag explicitly in environments where paid fallback is acceptable.

When `ALLOW_ANTHROPIC=0` and local LLM is unavailable, the API returns
`{"verdict": "error", "error": "llm_unavailable"}` rather than silently
degrading.

## Data source notes

### legalize-kr (Phase 1)
- 2,303 markdown files under `kr/`. One law per directory; one file per type
  (법률.md, 시행령.md, 시행규칙.md, ...).
- Each git commit corresponds to a real promulgation date. `git log -- kr/<법명>/`
  gives the revision history.
- Mount the corpus into the container at `/data/legalize-kr` (read-only).
- Multi-keyword queries are split on whitespace; OR markers (`OR`, `|`, `또는`)
  switch from `--all-match` (AND) to OR semantics.

### beopmang (Phase 1)
- Returns metadata only (counts, IDs) — useful for figuring out *which* law
  to read in detail before pulling full text.
- API responses wrap data in `{"data": {...}}` envelopes inconsistently —
  the client normalizes.

### chrisryugj/korean-law-mcp (Phase 2)
- HTTP client scaffold exists in `services/data/kolmcp_client.py`.
- Provides 16 MCP tools backed by 41 법제처 OpenAPIs, with
  citation-hallucination cross-validation.
- Plan: surface as a sub-source the API layer queries when the user asks for
  판례 / 행정규칙 / 헌재결정.

### SeoNaRu/lexguard-mcp (Phase 3)
- 18 MCP tools, 159 APIs, BM25+keyword reranker, 13-domain auto-classifier.
- Hosted endpoint: `https://lexguard-mcp.onrender.com/mcp` (also self-hostable).
- Plan: integrate alongside chrisryugj — they overlap but each has unique
  strengths (LexGuard reranker + contract analysis; chrisryugj citation guard).

## RLM Engine

Reference: [arXiv:2512.24601v2 — Recursive Language Models][rlm-paper].

Phase 1 (current): minimal loop with `exec()` sandbox + explicit
degradation policy when LLM is unavailable.

Phase 2 (planned): harden with [`RestrictedPython`][rpython] or full Docker
isolation. The threat model is "user-supplied query smuggles malicious
code through the LLM" — the LLM itself is trusted, but its code output isn't.

[rlm-paper]: https://arxiv.org/abs/2512.24601
[rpython]: https://restrictedpython.readthedocs.io

## Test strategy

| Layer | Approach |
|-------|----------|
| Source adapters | Fixture-based unit tests — no live network |
| API endpoints | FastAPI TestClient with mocked source adapters |
| LLM router | Dry-run mode (record prompts, no actual generation) |
| RLM | Mock `exec()` results, assert FINAL_ANSWER capture |
| Integration | Marked `@pytest.mark.integration`, run with `RUN_INTEGRATION=1` |

CI runs unit + API tests only. Integration tests are run locally before
release tags.

## Phase status

- ✅ **Phase 1** — scaffold, FastAPI app, schemas, legalize-kr local search,
  beopmang client, RLM minimal loop, 8 tests passing
- 🔧 **Phase 2** — chrisryugj/korean-law-mcp wiring (client scaffolded,
  blocked on OC API key for full integration tests)
- 📋 **Phase 3** — LexGuard MCP integration
- 📋 **Phase 4** — Full ChromaDB index of 2,303 statutes
- 📋 **Phase 5** — RLM sandbox hardening (RestrictedPython / Docker)
- 📋 **Phase 6** — kolaw-as-MCP-server (so this aggregator can be consumed
  from Claude Desktop / Cursor directly)
