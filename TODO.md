# kolaw — Operations Log

## 현재 운영 (2026-04-30)

```
Embedding:   jhgan/ko-sroberta-multitask  (collection: kolaw_laws, 130,608 chunks)
Reranker:    BAAI/bge-reranker-v2-m3      (KOLAW_USE_RERANKER=1, top_k=20)
Sub-LLM:     local Qwen3-32B → DeepSeek V4 (gated) → Claude (gated)
API:         :8100 (launchd com.user.kolaw.api)
MCP:         :3002 (launchd com.user.korean-law-mcp)
V&V (fast):  9/10
```

## BGE-m3 trial — 종료

- 시도: 2026-04-26~30, 14h overnight ingest로 `kolaw_laws_v2` 채울 계획
- 결과: 0.4% (10/2301 laws, ~768 chunks) 진행 후 강제 중단
- 결정: **접음 (옵션 B)** — 운영 ko-sroberta 9-10/10 충분, BGE-m3 도입 가치 미확인
- 정리: v2 + test collections 3개 drop, /tmp 로그 삭제, 스크립트 `_archive_*.bak` 보존

향후 재시도 조건: 운영 V&V 가 6/10 이하로 떨어지거나, 한국어 임베딩 SOTA가 명확히 갱신될 때.

## 좀비 watchdog

- `~/.claude/scripts/yholdings_unstick.sh` (60s launchd `com.user.yholdings-unstick`)
- 패턴 `etime>2h && cpu>300% && python.*chromadb` 자동 kill
- 정적 검증만 완료. 실기 시뮬은 차후.

## Discord ↔ ytower 라이브

- tmux `yholdings` → `claude --channels=plugin:discord` → hook → M1 Supabase
- 7개 에이전트 매핑: counsely / cap / bid / buildy / vital / growthy / legaly
- 인증: x-ingest-key + Discord access.json allowlist
- 봇 부팅 launcher: `~/.claude/scripts/yholdings_watchdog.sh`

## DeepSeek V4 통합 (대기)

- 코드 통합 완료 (router.py 3-tier), 키 미발급
- 활성화: DEEPSEEK_API_KEY 발급 → plist `ALLOW_DEEPSEEK=1`
- 가격: V4-Flash $0.14/$0.28 per 1M tokens

## 다음 세션 시작점

- 별도 일감 없음 — 운영 정상 안정 상태
- 발생 가능 작업:
  - 특허 케이스 reranker 미세 조정 (현재 `특허 출원 절차` → `디자인보호법` 매칭)
  - Discord OAuth 적용 (사용자 신원 일원화, 별도 결정 후)
  - DeepSeek API 키 발급 + smoke test
