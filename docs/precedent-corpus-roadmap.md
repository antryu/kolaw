# Precedent Corpus Roadmap — kolaw B.6.2

*작성일: 2026-05-06 | 의장 결재 필요: B.6.2 단계별 ingest*

---

## 현재 상태 (B.6.1 완료 기준)

| Collection | Source | 건수 | 상태 |
|---|---|---|---|
| `kolaw_laws_v3` | legalize-kr 2303 법령 | ~131,713 docs | 완료 (Phase B.1-3) |
| `kolaw_constitutional` | DRF detc — 헌재 결정 | 37,826건 | B.5.1 ingest 중 |
| `kolaw_precedents_supreme` | DRF prec — 5 법령 top | ~10,000건 | B.6.1 ingest 중 |
| `kolaw_precedents` | DRF prec — 5 법령 시범 | ~2,000건 | Phase B.4 완료 |

---

## Stage 계획 (B.6.2 이후)

### Stage 1: 핵심 법령 확장 (1-2개월, 의장 결재 필요)

**목표**: `kolaw_precedents_supreme` 확장 — 10,000 → 50,000건

**추가 법령 (top 500-1000건씩)**:
- 국민건강보험법 / 산업재해보상보험법 / 고용보험법 / 국민연금법 (4대 사회보험)
- 상법 / 어음수표법 (상사 분쟁)
- 소득세법 / 법인세법 / 부가가치세법 (세무 분쟁)
- 행정소송법 / 행정절차법 (행정 분쟁)
- 공정거래법 (경쟁법)
- 특허법 / 저작권법 (IP 분쟁)

**예상 소요**: 40,000건 × 2초/건(search+detail) ≈ 22시간 (rate-limited)
**디스크**: ~2GB ChromaDB 추가

### Stage 2: 대법원 전체 corpus (3-4개월, 별도 결재)

**목표**: DRF prec 172,340건 전수 ingest → `kolaw_precedents_full`

**방법**:
- `ingest_precedents_supreme.py`의 blank query(query=*) 모드로 전수 fetch
- 빈 쿼리 전체 scan: 172,340 × 2초 ≈ 96시간 (4일)
- 주말 background job 권장 (금요일 23:00 start → 화요일 완료 예상)

**디스크 추정**:
- 평균 chunk 2KB × 172,340건 = ~345MB documents
- ChromaDB embedding (1024-dim float32): 172,340 × 4KB ≈ 689MB
- 총 ~1.1GB 추가 (기존 2GB + 신규 1.1GB = 3.1GB)

**주의**: 기존 `kolaw_precedents_supreme` 와 중복 docs → dedup 처리 자동 적용

### Stage 3: 고등법원·지방법원 확장 (5-6개월, Out of scope for B.6)

**목표**: DRF prec 나머지 — 고등법원(6개) + 지방법원(18개) + 가정법원(17개)

현재 DRF prec에는 대법원 + 하급심 포함이나 하급심 coverage 불균등.
하급심 전용 crawl은 별도 법원 코드 기반 필터 필요 (DRF 지원 여부 미확인).

**대안**: 주요 고등법원 판결문만 ingest (서울고법, 대구고법 등 6개 법원)

---

## Cron 스케줄 (현재)

| Job | 시각 | 대상 | 설명 |
|---|---|---|---|
| `weekly-update.sh` | 일 02:00 | legalize-kr + 헌재 incremental | 기존 + B.5.2 추가 |
| `weekly-backup.sh` | 일 03:00 | ChromaDB 전체 백업 | 변경 없음 |

**B.6 incremental 추가 권장 (Stage 1 이후)**:
- 판례 weekly: `python -m services.fast_search.ingest_precedents_supreme --laws 의료법 근로기준법 --max-target 50` (신규 판례 top 50건만)
- 이를 `weekly-update.sh` 마지막에 추가

---

## Source 신뢰성

| Source | robots.txt | Rate limit | 법적 지위 |
|---|---|---|---|
| law.go.kr DRF (법령) | `/DRF` not disallowed | 1 req/sec | 법제처 공식 공개 API |
| law.go.kr DRF (prec) | Same | 1 req/sec | 법원행정처 공개 데이터 |
| law.go.kr DRF (detc) | Same | 1 req/sec | 헌법재판소 공개 결정문 |
| ccourt.go.kr | 허용 (JS WAF 차단) | N/A | 헌재 공식 — JS 필요 |

ccourt.go.kr 직접 접근: JavaScript 챌린지로 curl 불가. DRF detc target으로 대체 완료 (동일 데이터 소스).

---

## 의장 결재 필요 사항

- [ ] Stage 1 (핵심 법령 확장): 22시간 background run 승인 + ~2GB 디스크
- [ ] Stage 2 (전수 ingest): 4일 background run 승인 + ~1.1GB 추가 디스크
- [ ] 판례 weekly incremental cron 추가 (weekly-update.sh 수정)
- [ ] `kolaw_precedents` (B.4) + `kolaw_precedents_supreme` (B.6.1) 통합 또는 유지 결정

---

## 검색 통합

Legaly 검색 시 모든 collection 동시 query 권장:

```python
# apps/api/main.py 또는 search.py 확장 시:
COLLECTIONS_TO_SEARCH = [
    "kolaw_laws_v3",         # 법령
    "kolaw_constitutional",  # 헌재 결정
    "kolaw_precedents_supreme",  # 대법원 판례 (B.6.1+)
    "kolaw_precedents",      # 판례 시범 (B.4)
]
```

현재 `search.py`는 단일 `KOLAW_COLLECTION` 사용. multi-collection fan-out은 별도 결재 후 구현.
