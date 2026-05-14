# PageIndex + RLM PoC — 의료법

목적: kolaw 현 lawxref baseline vs PageIndex(XML 트리 reasoning) + RLM(Recursive Language Models, 자기비판 cycle) 정합성 비교.

## 결정 사항 (의장 default lock 2026-05-06)

| Q | 답 |
|---|---|
| Q1 baseline | `~/.claude/scripts/lawxref.sh` (production Legaly) |
| Q2 RLM evaluator | qwen2.5:14b 가 default 였으나 Ollama 미설치 → **llama-swap Qwen3-32B** 로 진행 (의장 사후 결재 필요) |
| Q3 PageIndex source | XML/Markdown 트리 (한국 법령 hierarchical) |
| Q4 Obsidian | `~/Documents/Obsidian Vault/Projects/y-Holdings/Strategy/` |
| Q5 코드 위치 | `~/PRJs/kolaw/eval/pageindex-rlm-poc/` |

## 작업 디렉토리

```
pageindex-rlm-poc/
├── README.md                  # 본 문서
├── build_tree.py              # Day 1~2: 의료법 markdown → tree JSON + mermaid
├── ask_kolaw.py               # Day 3: kolaw lawxref baseline 답변
├── ask_pageindex_rlm.py       # Day 3~4: PageIndex retrieve + Claude 1차 + RLM critique cycle
├── run_batch.py               # Day 5~6: 5질문 × 2시스템 batch
├── score_answers.py           # Day 7~8: Skepty 채점
├── tree/                      # 의료법 PageIndex JSON, mermaid
├── answers/                   # 각 질문 답변 trace
├── scoring/                   # Skepty 채점 결과
└── reports/                   # 5p 메모 (최종 산출물)
```

## 5 질문 (Q1~Q5 lock)

1. 의료법상 진료기록 보존기간은?
2. 위반 시 처벌 수위는?
3. 예외 사유 (보존기간 미적용 케이스) 있나?
4. 개정 이력 — 최근 5년 주요 변경점?
5. 관련 시행령·시행규칙 어디 있나?

## 실행 순서

```bash
cd ~/PRJs/kolaw/eval/pageindex-rlm-poc
source ~/PRJs/kolaw/.venv/bin/activate
python build_tree.py                    # Day 1~2
python run_batch.py                     # Day 3~6: 모든 답변 생성
python score_answers.py                 # Day 7~8: Skepty 채점
# 5p 메모는 reports/pageindex-rlm-poc-2026-05-07.md
```

## LLM 라우팅

- **Claude (1차 답변, 수정 답변)**: claude-opus-4-7-1m (의장 default)
- **Qwen3-32B llama-swap (RLM 비판자)**: `http://127.0.0.1:8080/v1/chat/completions`
  - Family Byzantine (Anthropic Claude vs Alibaba Qwen3) 충족
  - Q2 default Ollama qwen2.5:14b 와 다름 — 의장 사후 결재 필요

## 비용 monitoring

매 batch run 끝에 `answers/cost.json` 에 (input_tokens, output_tokens, USD) 기록.
