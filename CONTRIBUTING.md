# Contributing to kolaw

Thanks for your interest. kolaw is a small project with a clear scope:
**aggregate Korean law data sources behind one API.** Contributions that
improve search quality, source coverage, or developer experience are welcome.

## What contributions are most useful

| Area | Examples |
|------|----------|
| **Source adapters** | New data source clients (e.g. 헌법재판소 결정문 직접 크롤링), MCP wrappers |
| **Search quality** | Reranker tuning, query expansion, multi-keyword AND/OR, time-range filters |
| **Citation accuracy** | Article number parsing, cross-reference resolution, version pinning |
| **Test corpus** | Fixture statutes for offline tests, judgement number patterns (`2023다12345` 등) |
| **Docs / examples** | Quickstart for specific use cases (계약 검토, 규제 매핑, ...) |

## Development setup

```bash
git clone https://github.com/antryu/kolaw
cd kolaw

# Install (editable + dev deps)
pip install -e ".[dev]"

# Or via Docker for the full stack (API + ChromaDB)
docker compose up --build
```

Requires Python 3.12+. legalize-kr corpus is mounted from
`~/Thairon/legalize-kr` by default — adjust `LEGALIZE_KR_PATH` in `.env`.

## Run tests

```bash
pytest                          # full suite
pytest tests/test_health.py     # single file
pytest -k "legalize"            # filter by name
```

Tests use **fixture data** — they don't require live API access by default.
Tests that need network are marked with `@pytest.mark.integration` and skipped
in CI unless `RUN_INTEGRATION=1`.

## Code style

- **Type hints required** on public functions.
- **Pydantic models** for all API request/response shapes (see `apps/api/schemas.py`).
- **Async** for I/O (`httpx.AsyncClient`, FastAPI handlers).
- **Defensive parsing** — Korean law APIs return inconsistent shapes; assume nothing.

Format / lint (run before committing):
```bash
ruff check .
ruff format .
```

## Commit style

We follow a lightweight Conventional Commits flavor:

```
feat(scope): short description
fix(scope): ...
docs: ...
test: ...
refactor(scope): ...
```

Examples:
- `feat(legalize): add multi-keyword AND/OR search`
- `fix(beopmang): handle empty data envelope`
- `docs(readme): add LexGuard credit`

Scopes typically match service module names: `api`, `legalize`, `beopmang`,
`kolmcp`, `lexguard`, `rlm`, `llm`, `chroma`.

## LLM policy

kolaw defaults to **local LLMs** (Qwen3-32B via llama.cpp / llama-swap).
Anthropic API access is gated behind `ALLOW_ANTHROPIC=1` and intended for
maintainer-approved workflows only — please don't add code paths that
require paid LLM access without local fallback.

## Reporting issues

When filing issues:
- Include the query that triggered unexpected results
- Mention which data source was consulted (check the `source` field in responses)
- For incorrect citations, paste the offending response and the expected output

Sensitive items (e.g. API keys leaked in logs) — please email the maintainer
privately rather than opening a public issue.

## License

By contributing, you agree your contributions will be licensed under the
[MIT License](LICENSE).
