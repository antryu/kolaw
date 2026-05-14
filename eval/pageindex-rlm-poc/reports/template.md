# PageIndex + RLM PoC — 의료법 1개 법령 (5p 메모)

작성: Buildy (R&D 임시) · 검토: Counsely · 채점: Skepty
일자: 2026-05-07

---

## Page 1 — 결론 + 의장 결재

**한 줄**: PageIndex(트리 retrieve) + RLM(Claude↔Qwen3-32B 자기비판 cycle) 가
production kolaw lawxref 보다 **정합성 합계 +X점 / 키워드 적중 +YY%p / 인용 정확도 +ZZ%p**.
부분 도입 권고.

| 결재 항목 | yes / no |
|---|---|
| A. kolaw 전체를 PageIndex 로 재설계 | □ / □ |
| B. lawxref 위 PageIndex 레이어 추가 (병렬 호출, A/B test 30일) | □ / □ |
| C. RLM critique 만 Legaly opt-in (deep mode) | □ / □ |
| D. PoC 보류, Phase 2 변경 없음 | □ / □ |

권고: **B + C** (A 는 비용·복잡도 너무 큼, D 는 변호사 정합성 issue 방치).

리스크:
- Q2 default lock 위반 — Ollama qwen2.5:14b 미설치 → llama-swap Qwen3-32B 대체. 사후 결재 필요.
- PageIndex retrieval 도 hallucination 가능 (트리 navigation 단계에서 Claude 가 잘못 고르면 동일 한계).
- RLM cycle 1회 추가 ≈ Claude 호출 3회 + Qwen 1회 → 응답 latency 70~120초 (deep mode 만 적합).

---

## Page 2 — 의료법 PageIndex 트리

(트리 stats + mermaid + 텍스트 outline 발췌)

---

## Page 3 — 5 질문 비교 표

| qid | 질문 | kolaw 합계 | PI+RLM 합계 | kolaw kw% | PI+RLM kw% | kolaw 인용 | PI+RLM 인용 | kolaw lat | PI+RLM lat |
|---|---|---|---|---|---|---|---|---|---|

---

## Page 4 — 정합성 채점 분포 + 핵심 finding

(점수 분포 도표 + 양 시스템 차이가 가장 컸던 질문 case study)

---

## Page 5 — 권고 (kolaw 전체 재설계 / 부분 적용 / 보류)

(Page 1 권고 근거 상세)

### 다음 step 후보

1. RLM critique 만 deep mode 옵션으로 production kolaw 에 추가 (1~2주)
2. PageIndex 트리 retrieve 5개 법령 확장 PoC (의료법 + 자본시장법 + 국가계약법 + 약사법 + 노동기준법)
3. Skepty 자동 채점을 production kolaw answer pipeline 에 hook
