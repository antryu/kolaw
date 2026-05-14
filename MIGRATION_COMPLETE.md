# kolaw Migrated to M1 (2026-05-07)

이 폴더 (`~/PRJs/kolaw` on m4max) 는 **archive 상태**.

**Canonical 위치**: M1 (Tailscale `m1` host) `~/Thairon/kolaw/`
**API**: M1 `:8100` (LaunchAgent `com.user.kolaw.api` 자동 restart)
**ChromaDB**: M1 `~/Thairon/kolaw/services/fast_search/chroma_db/` (4.8GB, v3 + constitutional)

**규칙**:
- 이 폴더에 새 commit / push 금지
- 코드 수정은 M1 ~/Thairon/kolaw 에서만
- 단 PoC 작업 (`eval/pageindex-rlm-poc/`) 은 m4max 에서 완료된 결과 (read-only)

**배경**: m4max sleep 시 kolaw down → y-Tower (M1) 의 Legaly · /comparison 답변 X. 24/7 운영 위해 M1 으로 이전.

**관련 메모리** (Counsely):
- `kolaw_infra.md` (M1 path · bring-up 갱신)
- `ytower_always_m1.md` (모든 작업 M1)
- `no_anthropic_api_key.md` (LLM = Claude CLI subprocess)

migrated by Buildy (executor sonnet) at 2026-05-06 via Counsely 위임.
