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

### law.go.kr Open API (primary live source)
- Direct calls to `https://www.law.go.kr/DRF/lawSearch.do` via
  `services/data/law_go_kr.py`. No MCP indirection.
- Single `OC` query parameter is the project-name you registered at
  open.law.go.kr — **not a secret**, treat like a username.
- Six target codes work out of the box with any registered OC:
  `law / prec / expc / decc / admrul / ordin`.
- One target needs separate permission (see below): `detc` (헌재 결정).

#### 헌법재판소 결정 — superseded by data.go.kr
The law.go.kr `detc` target requires a separate manual permission flow
(신청관리 → 사용중지/추가신청 → 헌법재판소 결정, ~1 business day approval).
We no longer recommend that path; instead we use the dedicated
**data.go.kr 헌법재판소 판례 API**, which is auto-approved on registration
and exposes individual decisions with case numbers (`2020헌마956` style).

Setup:
1. <https://www.data.go.kr/data/15123093/openapi.do> → 활용신청 (auto-approved on the spot)
2. Copy the Decoding key from your portal profile
3. `DATA_GO_KR_KEY=<key>` in `.env`

The client in `services/data/data_go_kr_court.py` smart-routes inside
fast_search: only fires when the query mentions a constitution keyword
(헌법 / 헌재 / 위헌 / 합헌 / 기본권 / 탄핵 / 권한쟁의 / 정당해산 / etc.)
so we don't burn the 1000/day budget on unrelated searches.

The eventNm filter is a literal substring match against the case
nickname, so multi-word queries get whittled down to the first
non-stopword keyword (stripping "위헌" / "헌법" etc.) before the call.

If `law_go_kr.py` ever does receive `detc` permission later, both
sources can coexist — they return different shapes and the merge logic
in fast_search handles dedup-by-(law_name, article).

### chrisryugj/korean-law-mcp (optional)
- Wraps 16 of the 41 법제처 APIs as MCP tools, plus its headline
  `verify_citations` (LLM hallucination check) and chain orchestration tools.
- We hit the underlying APIs directly via `law_go_kr`, so the only reason to
  also run chrisryugj is its **verify_citations** and **chain_full_research**
  tools — those do reasoning the raw OpenAPI doesn't.
- Setup (5 minutes once you have an OC):
  ```bash
  npm install -g korean-law-mcp
  LAW_OC=$LAW_GO_KR_OC korean-law-mcp --http --port 3001 &
  ```
- `services/data/kolmcp_client.py` reuses `LAW_GO_KR_OC` automatically;
  override with `KOLMCP_OC` only if you want a separate identity.
- The client tries JSON-RPC 2.0 (`/mcp`) first, falls back to the legacy
  REST-style `/tools/<name>` shape if the server fork uses that.

### SeoNaRu/lexguard-mcp (optional)
- 18 MCP tools / 159 APIs / BM25+keyword reranker / 13-domain auto-classifier
  / contract analyzer.
- Hosted endpoint at `https://lexguard-mcp.onrender.com/mcp` is reachable but
  its upstream OC is missing, so it returns empty results. Self-host for real
  data:
  ```bash
  git clone https://github.com/SeoNaRu/lexguard-mcp
  LAW_API_KEY=$LAW_GO_KR_OC docker compose up --build
  # MCP endpoint at http://localhost:9099/mcp
  export LEXGUARD_BASE_URL=http://localhost:9099/mcp
  ```
- The `services/data/lexguard_client.py` JSON-RPC client handles both the
  hosted endpoint (for sniff tests) and a self-host (for real data).

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
