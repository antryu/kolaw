# kolaw — Korean Law Aggregator

> **Open-source aggregator for Korean law data sources.**
> One API to search 한국 법령·판례·해석·헌재결정, combining offline statute corpus with the best-in-class MCP servers.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.124+-009688.svg)](https://fastapi.tiangolo.com)

---

## 🇰🇷 한국어 안내

**kolaw** 는 한국 법령 검색을 한 곳에 모은 오픈소스 백엔드입니다.

- **legalize-kr** (오프라인 git repo, 2,300+ 법령) + **법제처 Open API** (실시간 판례·해석·행정규칙·조례) 를 단일 `/search` HTTP 엔드포인트로 통합
- 키워드 검색은 다중키워드 **AND/OR** 지원 — `의료 규제` (AND) / `의료 OR 외국인` / `의료 \| 외국인` / `의료 또는 외국인` (OR)
- chrisryugj/korean-law-mcp · SeoNaRu/lexguard-mcp 같은 기존 MCP 서버는 **선택적 통합** (필요할 때만)
- **LLM 기반 자율 검색 (RLM)** 은 옵션 — 로컬 Qwen3-32B 같은 OpenAI 호환 엔드포인트를 사용

### 본인이 직접 준비해야 하는 것

| 항목 | 용도 | 소요 시간 |
|---|---|---|
| **OC 등록** at <https://open.law.go.kr> | 법제처 Open API 호출 — 6개 source 즉시 활성화 (법령/판례/해석/행심/행정규칙/조례) | 회원가입 + 프로젝트명 등록 ~5분 (즉시 승인) |
| **헌법재판소 결정 추가 신청** | 7번째 source `detc` (헌재 결정) 활성화 | 신청관리 → 사용중지/추가신청 → 헌법재판소 결정 체크 → 약 1일 승인 대기 |
| **legalize-kr 로컬 클론** | 오프라인 법령 본문 + 개정 이력 (git log) | `git clone github.com/9bow/legalize-kr` ~3분 |
| (옵션) **로컬 LLM** | Deep 모드 (RLM 엔진) — Fast 모드는 LLM 없이 작동 | llama.cpp / Ollama / llama-swap 등 |

⚠️ **`OC` 값은 비밀 키가 아니라 본인이 정한 프로젝트 이름** (사용자명처럼 다루세요). 코드 안에 baked-in된 키 없음 — 본인이 직접 등록해서 `LAW_GO_KR_OC` 환경변수에 넣으세요.

영문 안내는 아래 ↓

---

## Why kolaw?

Korean law tooling has matured rapidly — multiple excellent MCP servers exist
([korean-law-mcp][chrisryugj], [LexGuard][lexguard]) — each with its own strengths.
**kolaw doesn't replace them. It unifies them.**

- **Offline statute corpus** via [legalize-kr][legalize-kr] (MIT) — 2,300+ laws as markdown,
  works without an API key, with full **revision history via `git log`** ("what did this
  law say in 2020?").
- **Live 판례·해석·행정규칙·조례** via [law.go.kr Open API][lawgokr-open] — direct calls,
  no MCP indirection. Just register a project name (the `OC` value).
- **Multi-source unified search** — `/search` runs offline grep + live API in parallel,
  returns merged citations.
- **Multi-keyword AND/OR** — `"의료 규제"` (AND) or `"의료 OR 외국인"` / `"의료 \| 외국인"` /
  `"의료 또는 외국인"` (OR). All shorthand recognized.
- **LLM-driven autonomous search (RLM)** for queries pure keyword search can't handle —
  experimental, requires local LLM.

## Architecture

```
              ┌──────────────────────────────────────┐
              │           antryu/kolaw                │
              │      FastAPI on :8100                 │
              │  /search  /search/batch  /health      │
              └──────────────────────────────────────┘
                              │
   ┌──────────────────┬───────┴────────┬─────────────────────┐
   ▼                  ▼                ▼                     ▼
┌──────────────┐  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐
│ legalize-kr  │  │ law.go.kr   │  │ lexguard-mcp │  │ korean-law-mcp   │
│ git grep     │  │ Open API    │  │ (optional,   │  │ (optional,       │
│ 2,300+ laws  │  │ 판례·해석·   │  │ self-host)   │  │ self-host)       │
│ AND/OR       │  │ 행정규칙·조례 │  │ reranker +   │  │ 64 tools,        │
│ + 개정 이력  │  │ 6 sources    │  │ contract     │  │ citation verif.  │
│ MIT, offline │  │ OC=YourName  │  │ analysis     │  │                  │
└──────────────┘  └─────────────┘  └──────────────┘  └──────────────────┘
        │                │                │                     │
        └────────────────┴────────────────┴─────────────────────┘
                              │
                  ┌───────────┴───────────┐
                  │ Fast path (default)   │  legalize-kr grep + law.go.kr merge
                  │ Deep path (RLM)       │  Qwen3 / Claude → REPL → citations
                  └───────────────────────┘
```

| Source | Coverage | License | Status |
|--------|----------|---------|--------|
| [9bow/legalize-kr][legalize-kr] | 2,300+ 법률·시행령·시행규칙 (offline + git history) | MIT | ✅ live |
| [law.go.kr Open API][lawgokr-open] | 법령 + 판례 + 법령해석례 + 행정심판 + 행정규칙 + 조례 | data.go.kr | ✅ live |
| [api.beopmang.org][beopmang] | Structured metadata (article/case counts) | — | ✅ wired |
| [SeoNaRu/lexguard-mcp][lexguard] | 18 MCP tools / 159 APIs (reranker, contract analyzer) | MIT | 🧩 optional |
| [chrisryugj/korean-law-mcp][chrisryugj] | 16 MCP tools / 41 APIs (citation verification) | — | 🧩 optional |

**Two search paths:**
- **Fast** (default): legalize-kr grep + law.go.kr live merge → typically <2s, no LLM cost
- **Deep**: RLM Engine — local LLM writes Python in REPL, calls grep + law.go.kr as tools,
  emits `FINAL_ANSWER`. Requires `LOCAL_LLM_BASE_URL`. Experimental.

## Prerequisites — what you need to bring yourself

kolaw is a thin aggregator. It wraps services that **require your own
registration / setup**. Nothing here is shipped with credentials baked in.

### Required for live data

| You provide | What for | How long | Where |
|-------------|----------|----------|-------|
| **`LAW_GO_KR_OC`** — a project name registered at open.law.go.kr | Unlocks 6 of 7 live sources: 법령 / 판례 / 법령해석례 / 행정심판 / 행정규칙 / 자치법규 | ~5 min to register; instant approval | <https://open.law.go.kr/LSO/openApi/guideList.do> |
| `legalize-kr` repo cloned locally | Offline 2,300+ statute corpus + git revision history | ~3 min | `git clone github.com/9bow/legalize-kr` |

The `OC` value is **not a secret** — it's the project name you chose on
open.law.go.kr (treat it like a username). All this repo's code paths
that hit law.go.kr fail with a clear "OC not configured" message until
you set it.

### Required for specific sources / modes

| You provide | Unlocks | Cost | Note |
|-------------|---------|------|------|
| **헌법재판소 결정 추가 권한** at open.law.go.kr | The 7th live source: `detc` (헌재 결정) | Free, ~1 day approval | Login → 신청관리 → 사용중지/추가신청 → check 헌법재판소 결정 → submit |
| Self-hosted `korean-law-mcp` (chrisryugj) | `verify_citations` (citation hallucination check), `chain_full_research` | Free, ~5 min | `npm i -g korean-law-mcp` then `LAW_OC=$LAW_GO_KR_OC korean-law-mcp --mode http --port 3001` |
| Self-hosted `lexguard-mcp` (SeoNaRu) | Reranker, 13-domain classifier, contract analyzer (18 tools / 159 APIs) | Free, ~10 min | `git clone github.com/SeoNaRu/lexguard-mcp && LAW_API_KEY=$LAW_GO_KR_OC docker compose up` |
| Local LLM (e.g. Qwen3-32B via llama.cpp) | Deep mode — RLM Engine | Free, ~30 min download | Any OpenAI-compatible endpoint at `LOCAL_LLM_BASE_URL` |
| Anthropic API key | Deep mode fallback when local LLM is down | Paid | `ALLOW_ANTHROPIC=1` + `ANTHROPIC_API_KEY=...` (off by default; gated) |

### Not required (works without)

- Supabase / database — kolaw is stateless; nothing is persisted server-side
- ChromaDB / vector index — currently not used (keyword + live API combo
  handles the workload). The deps remain in `pyproject.toml` for the optional
  Phase-4 path, but no vectors are built or queried by default.

> **TL;DR — bare minimum to get useful answers:** register an `OC` at
> open.law.go.kr (5 minutes) + clone legalize-kr. Everything else is opt-in.

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
# law.go.kr Open API — register a project name at open.law.go.kr,
# then use that name as the OC value. NOT a secret.
LAW_GO_KR_OC=YourRegisteredProjectName

# legalize-kr corpus mount (clone github.com/9bow/legalize-kr)
LEGALIZE_KR_PATH=/data/legalize-kr

# LLM (primary: local Qwen3-32B via llama.cpp / llama-swap; Deep mode only)
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1
LOCAL_LLM_MODEL=qwen3:32b

# Optional Anthropic fallback (gated)
ALLOW_ANTHROPIC=0

# Optional supplementary metadata
BEOPMANG_BASE_URL=https://api.beopmang.org/api/v4

# Optional MCP integrations
# LEXGUARD_BASE_URL=http://localhost:9099/mcp   # self-hosted lexguard-mcp
# KOLMCP_BASE_URL=http://localhost:3001         # self-hosted korean-law-mcp
```

> **헌법재판소 결정 (`detc`)** is gated behind a separate API permission at
> open.law.go.kr — a bare OC registration returns an empty schema-only
> response for that target. To enable it, log in at open.law.go.kr →
> 신청관리 → 사용중지/추가신청 → check 헌법재판소 결정 → submit. Approval
> typically takes 1 business day. See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)
> for the full procedure. The five other live sources (법령·판례·해석·행심·
> 행정규칙·자치법규) work out of the box with any registered OC.

## Roadmap

**Done**
- [x] legalize-kr local loader (article parsing, frontmatter, git history)
- [x] Multi-keyword **AND / OR** keyword search via `git grep` (`OR` / `|` / `또는`)
- [x] **law.go.kr Open API direct client** — 6 sources: 법령 / 판례 / 법령해석례 /
  행정심판 재결 / 행정규칙 / 자치법규 (조례). Single `OC` parameter unlocks all.
- [x] `/search` merges legalize-kr grep + law.go.kr live results in parallel,
  dedupes by (law_name, article)
- [x] `/health` surfaces every data source's reachability + config status
- [x] LexGuard MCP JSON-RPC client (typed wrappers; ready for self-host)
- [x] beopmang client (metadata enrichment)
- [x] RLM minimal loop on top of grep-based prefilter (no ChromaDB dependency)
- [x] Test suite: 34 passing

**Pending**
- [ ] 헌법재판소 결정 (`detc`) — needs separate API permission registration at
  open.law.go.kr beyond the base `OC` value
- [ ] chrisryugj/korean-law-mcp self-host wiring (client scaffolded; needs the
  caller to run the MCP server locally with their own OC key)
- [ ] LexGuard self-host integration tests (hosted endpoint returns empty
  results because its upstream OC key is missing)
- [ ] Optional ChromaDB vector index (`sentence-transformers`) — deferred; the
  current keyword + live-API combo handles most queries without it
- [ ] RLM production hardening — multi-turn retries when the LLM emits
  syntactically broken Python; `RestrictedPython` / Docker sandbox for the REPL
- [ ] kolaw-as-MCP-server wrapper — expose `/search` as an MCP tool so Claude
  Desktop / Cursor can consume kolaw directly

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
