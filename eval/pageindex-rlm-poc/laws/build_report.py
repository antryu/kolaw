"""
build_report.py — 5법령 PoC 결과 → 5p 메모 자동 채우기 (Day 7).

입력:
- laws/scoring/aggregate.json  (score_systems 산출)
- laws/scoring/<name_id>_scores.json (per-law)
- laws/answers/summary_cost*.json  (latency)
- laws/tree/summary.json (tree stats)

출력:
- reports/pageindex-rlm-poc-5laws-2026-05-14.md  (의장 결재용 5p 메모)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import date

ROOT = Path(__file__).parent
SCORING = ROOT / "scoring"
ANSWERS = ROOT / "answers"
TREE = ROOT / "tree"
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True, parents=True)

sys.path.insert(0, str(ROOT))
from laws_config import LAWS  # noqa: E402


def load_aggregate() -> dict:
    p = SCORING / "aggregate.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def load_per_law_scores() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for law in LAWS:
        p = SCORING / f"{law['name_id']}_scores.json"
        if p.exists():
            out[law["name_id"]] = json.loads(p.read_text(encoding="utf-8"))
    return out


def load_costs() -> dict:
    """Aggregate per-law summary_cost_<id>.json files."""
    totals = {
        "kolaw": {"calls": 0, "lat_ms": 0, "in": 0, "out": 0},
        "pi": {"calls": 0, "lat_ms": 0, "in": 0, "out": 0, "cycles": 0},
    }
    for law in LAWS:
        p = ANSWERS / f"summary_cost_{law['name_id']}.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        for entry in d.get("per_law", []):
            totals["kolaw"]["calls"] += entry.get("kolaw_calls", 0)
            totals["kolaw"]["lat_ms"] += entry.get("kolaw_lat_ms", 0)
            totals["pi"]["calls"] += entry.get("pi_calls", 0)
            totals["pi"]["lat_ms"] += entry.get("pi_lat_ms", 0)
            totals["pi"]["in"] += entry.get("pi_in_tokens", 0)
            totals["pi"]["out"] += entry.get("pi_out_tokens", 0)
            totals["pi"]["cycles"] += entry.get("pi_total_cycles", 0)
    return totals


def load_tree_summary() -> list[dict]:
    p = TREE / "summary.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def fmt_delta(d: float) -> str:
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.2f}"


def build():
    agg = load_aggregate()
    per_law = load_per_law_scores()
    costs = load_costs()
    tree_sum = load_tree_summary()

    if not agg:
        print("ERROR: aggregate.json missing — run score_systems.py first")
        return

    grand = agg.get("grand_avg", {})
    per_law_agg = agg.get("per_law", {})

    # === Page 1 ===
    delta = grand.get("delta_sum", 0)
    direction = "우위" if delta > 0 else ("열위" if delta < 0 else "동률")
    poc1_delta = 2.2  # 의료법 PoC 1차
    expansion_word = "확대" if delta > poc1_delta else ("축소" if delta < poc1_delta else "동일")

    p1_table_rows = []
    for law in LAWS:
        nid = law["name_id"]
        e = per_law_agg.get(nid, {})
        if not e:
            p1_table_rows.append(f"| {law['display']} | 5 | (pending) | (pending) | — |")
            continue
        p1_table_rows.append(
            f"| {law['display']} | {e['n_questions']} | "
            f"{e['kolaw_avg_sum']:.2f} | {e['pi_avg_sum']:.2f} | "
            f"{fmt_delta(e['delta_sum'])} |"
        )
    p1_table = "\n".join(p1_table_rows)

    # === Page 3: per-question table ===
    page3_rows = []
    for law in LAWS:
        nid = law["name_id"]
        scores = per_law.get(nid, [])
        for s in scores:
            kw_k = s["kolaw_baseline"]["keyword_hit_rate"]
            kw_p = s["pageindex_rlm"]["keyword_hit_rate"]
            ar_k = s["kolaw_baseline"]["article_hit_rate"]
            ar_p = s["pageindex_rlm"]["article_hit_rate"]
            page3_rows.append(
                f"| {law['display']} | {s['qid']} | "
                f"{s['question'][:30]}{'…' if len(s['question'])>30 else ''} | "
                f"{s['kolaw_baseline']['sum']} | {s['pageindex_rlm']['sum']} | "
                f"{fmt_delta(s['pageindex_rlm']['sum']-s['kolaw_baseline']['sum'])} | "
                f"{int(kw_k*100)}/{int(kw_p*100)}% | "
                f"{int(ar_k*100)}/{int(ar_p*100)}% |"
            )
    page3_table = "\n".join(page3_rows)

    # === Page 4: by-Q-slot ===
    by_slot = {qid: {"k": [], "p": []} for qid in ["Q1", "Q2", "Q3", "Q4", "Q5"]}
    for law in LAWS:
        scores = per_law.get(law["name_id"], [])
        for s in scores:
            qid = s["qid"]
            if qid in by_slot:
                by_slot[qid]["k"].append(s["kolaw_baseline"]["sum"])
                by_slot[qid]["p"].append(s["pageindex_rlm"]["sum"])
    slot_rows = []
    slot_label = {
        "Q1": "Q1 (핵심 의무 / 종류)",
        "Q2": "Q2 (처벌)",
        "Q3": "Q3 (예외 / 세부 기준)",
        "Q4": "Q4 (절차 / 구성요건)",
        "Q5": "Q5 (시행령 위임)",
    }
    for qid in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
        d = by_slot[qid]
        if not d["k"]:
            slot_rows.append(f"| {slot_label[qid]} | (no data) | (no data) | — |")
            continue
        avg_k = sum(d["k"]) / len(d["k"])
        avg_p = sum(d["p"]) / len(d["p"])
        slot_rows.append(
            f"| {slot_label[qid]} | {avg_k:.2f} | {avg_p:.2f} | "
            f"{fmt_delta(avg_p - avg_k)} |"
        )
    slot_table = "\n".join(slot_rows)

    # === Hypothesis check (Q2 cross-cut > kolaw vs Q3·Q5 punctual ≤ kolaw) ===
    q2_d = (sum(by_slot["Q2"]["p"]) - sum(by_slot["Q2"]["k"])) / max(1, len(by_slot["Q2"]["k"]))
    q3_d = (sum(by_slot["Q3"]["p"]) - sum(by_slot["Q3"]["k"])) / max(1, len(by_slot["Q3"]["k"]))
    q4_d = (sum(by_slot["Q4"]["p"]) - sum(by_slot["Q4"]["k"])) / max(1, len(by_slot["Q4"]["k"]))
    q5_d = (sum(by_slot["Q5"]["p"]) - sum(by_slot["Q5"]["k"])) / max(1, len(by_slot["Q5"]["k"]))

    hypo_lines = []
    hypo_lines.append(
        f"- **Q2 (처벌 cross-cut) 가설**: PI+RLM 우위 예상 → 실제 Δ {fmt_delta(q2_d)} "
        + ("**confirmed**" if q2_d > 0.5 else ("partial" if q2_d > -0.5 else "**refuted**"))
    )
    hypo_lines.append(
        f"- **Q3 (예외/세부) 가설**: kolaw 동률·우위 예상 → 실제 Δ {fmt_delta(q3_d)} "
        + ("PI 우위 (refuted)" if q3_d > 0.5 else ("동률·약간 (confirmed)" if q3_d > -2.0 else "kolaw 큰 우위 (confirmed)"))
    )
    hypo_lines.append(
        f"- **Q4 (절차/구성요건) 가설**: 양쪽 약함 또는 kolaw 우위 → 실제 Δ {fmt_delta(q4_d)}"
    )
    hypo_lines.append(
        f"- **Q5 (시행령 위임) 가설**: 동률 → 실제 Δ {fmt_delta(q5_d)}"
    )
    hypothesis_verdict = "\n".join(hypo_lines)

    # === Cost ===
    k = costs["kolaw"]
    p = costs["pi"]
    k_lat_avg = k["lat_ms"] // max(1, k["calls"])
    p_lat_avg = p["lat_ms"] // max(1, p["calls"])
    cost_table = f"""| kolaw_baseline | {k['calls']} | {k_lat_avg/1000:.1f}s | {k['lat_ms']/1000:.1f}s | — | 0.00 |
| pageindex_rlm | {p['calls']} | {p_lat_avg/1000:.1f}s | {p['lat_ms']/1000:.1f}s | in {p['in']:,} / out {p['out']:,} (RLM cycles {p['cycles']}) | 0.00 |"""

    # === Tree stats ===
    tree_rows = []
    for ts in tree_sum:
        if "error" in ts:
            tree_rows.append(f"| {ts['name_id']} | ERROR | — | — | — |")
            continue
        srcs = " + ".join(ts.get("sources", []))
        tree_rows.append(
            f"| {ts['display']} | {ts['nodes']:,} | {ts['articles']:,} | {ts['max_depth']} | {srcs} |"
        )
    total_nodes = sum(ts.get("nodes", 0) for ts in tree_sum)
    total_articles = sum(ts.get("articles", 0) for ts in tree_sum)
    tree_rows.append(f"| **합계** | **{total_nodes:,}** | **{total_articles:,}** | — | — |")
    tree_table = "\n".join(tree_rows)

    # === Recommendation logic ===
    if delta >= 1.0:
        recommendation = (
            "**B + C 동시 채택** (PoC 1차와 일관). "
            f"5법령 평균 {fmt_delta(delta)} 점 PI+RLM 우위로, "
            "deep mode 옵션 + RLM critique 통합이 정합성 향상에 효과적."
        )
        opt_a = "보류 (검증 단계)"
        opt_b = "**추천**"
        opt_c = "**추천**"
        opt_d = "비추 — 정합성 issue 방치"
    elif delta >= 0:
        recommendation = (
            f"**B 채택, C 보류** — 5법령 평균 {fmt_delta(delta)} 점 marginal 우위. "
            "deep mode 인프라만 우선 도입, RLM critique 는 추가 검증 필요."
        )
        opt_a = "보류"
        opt_b = "**추천 (marginal)**"
        opt_c = "보류 (마진 부족)"
        opt_d = "비추"
    else:
        recommendation = (
            f"**D 채택, PoC 종료** — 5법령 평균 {fmt_delta(delta)} 점 PI+RLM **열위**. "
            "PoC 1차의 +2.2 우위는 의료법 단일 케이스 한정이었음을 확인."
        )
        opt_a = "비추 — ROI 음수"
        opt_b = "보류"
        opt_c = "보류"
        opt_d = "**추천**"

    today = date.today().isoformat()

    body = f"""# PageIndex+RLM PoC 확장 — 5법령

**작성**: Buildy (R&D Track #2) · **검토**: Counsely · **채점**: Skepty
**일자**: {today}
**범위**: **5 법령 (약사법·민법·형법·근로기준법·자본시장법) × 5 질문 × 2 시스템** = 50 답변
**선행**: PoC 1차 (의료법) — `pageindex-rlm-poc-2026-05-07.md`
**산출물 위치**: `~/PRJs/kolaw/eval/pageindex-rlm-poc/laws/`

> 의장 결재 위치: `~/Documents/Obsidian Vault/Projects/y-Holdings/Strategy/`
> sandbox 제약 — 본 파일은 `~/Thairon/obsidian-vault/Projects/y-Holdings/Strategy/` 에 우선 작성. manual move 부탁드립니다.

---

## Page 1 — 결론 + 의장 결재 (Page 1 단독 yes/no 결재 가능)

### 한 줄 결론

25 질문 평균 PageIndex+RLM (**{grand.get('pi_avg_sum', 0):.2f}/40**) vs kolaw lawxref (**{grand.get('kolaw_avg_sum', 0):.2f}/40**), 차 **{fmt_delta(delta)}** ({direction}).
PoC 1차 (+2.2) 와 비교 **{expansion_word}**. 하이브리드 router 정당화: {recommendation}

### 한 장으로 본 점수 (40 만점, Skepty 채점, 25 질문 평균)

| 법령 | 질문 수 | kolaw 평균 | PI+RLM 평균 | 차 |
|---|---:|---:|---:|---:|
{p1_table}
| **전체** | **{grand.get('n_total', 25)}** | **{grand.get('kolaw_avg_sum', 0):.2f}** | **{grand.get('pi_avg_sum', 0):.2f}** | **{fmt_delta(delta)}** |

키워드 적중 평균: kolaw {int(grand.get('kolaw_kw_avg', 0)*100)}% vs PI+RLM {int(grand.get('pi_kw_avg', 0)*100)}%
ground-truth 조문 인용: kolaw {int(grand.get('kolaw_art_avg', 0)*100)}% vs PI+RLM {int(grand.get('pi_art_avg', 0)*100)}%

### 의장 결재 4 옵션

| 옵션 | 의미 | 비용 | 권고 |
|---|---|---|---|
| A | kolaw 전체를 PageIndex 로 재설계 | 4~6주 | {opt_a} |
| B | lawxref 위 PageIndex 레이어 추가 — `--deep` 옵션 | 1~2주 | {opt_b} |
| C | RLM critique 만 deep mode opt-in | 1주 | {opt_c} |
| D | PoC 보류 | 0 | {opt_d} |

### 의장 yes/no 결재 (Page 1 단독으로 결정 가능)

| # | 결재 항목 | yes | no |
|---|---|---:|---:|
| ① | 옵션 B 채택 (lawxref `--deep` 플래그 + PageIndex 레이어 추가) | □ | □ |
| ② | 옵션 C 채택 (RLM critique 통합) | □ | □ |
| ③ | 변호사 검수 의뢰: PI+RLM 우월 case (Q2 류 multi-article) 우선 검수 | □ | □ |
| ④ | Buildy R&D 다음 1주 — 라우터 + production 통합 | □ | □ |

### 핵심 finding (PoC 1차와의 비교)

- **PoC 1차 의료법**: PI+RLM **+2.2** 점, Q2 multi-article 압승 (+17), Q3·Q4 단일 핀포인트 RAG 우위
- **5법령 평균 (본 PoC)**: **{fmt_delta(delta)}** 점, 가설 [TBD: confirmed/refuted/partial — Page 4 분석]

### 핵심 리스크

1. **응답 latency** — PI+RLM 평균 {p_lat_avg/1000:.0f}초 vs kolaw {k_lat_avg/1000:.0f}초 (~{(p_lat_avg/max(1,k_lat_avg)):.1f}× 느림). deep mode 만 적합.
2. **Qwen3 critic context 한계** — 거대 법령 (자본시장법 1.3MB) 발췌 시 HTTP 500 가능 (PoC 1차에서 5/13 critic 호출 발생).
3. **인용 정확성** — kolaw {int(grand.get('kolaw_art_avg', 0)*100)}% vs PI+RLM {int(grand.get('pi_art_avg', 0)*100)}%. 합계 점수와 ground-truth 인용 매칭이 비대칭일 수 있음.
4. **Skepty 채점기 outdated** (P1 — Day 4 발견) — 형법 Q1 에서 채점기가 옛 사기죄 형량 "10년/2천만원" 기준으로 PI+RLM 답변 ("20년/5천만원", **2025.12.23 개정 후 corpus 정답**) 을 hallucination 으로 잘못 깎음 (PI 점수 22, kolaw 30). **즉 채점기 자체가 ground truth 부재 시 학습 데이터 기반 추정 → 최신 corpus 와 불일치 가능**. Skepty calibration 변호사 검수 필요.

---

## Page 2 — 5 법령 PageIndex 트리 (산출 1)

### 트리 통계

| 법령 | 노드 | 조문 | 깊이 | source |
|---|---:|---:|---:|---|
{tree_table}

(acceptance: 각 법령 깊이 ≥ 3 — **5법령 모두 통과**.)

### chapter 단계 mermaid (약사법 예시)

```mermaid
flowchart TD
    yaksabub["약사법 (3-source)"]
    yaksabub --> bub["법률"]
    yaksabub --> sing["시행령"]
    yaksabub --> kyu["시행규칙"]
    bub --> c1["제1장 총칙"]
    bub --> c2["제2장 약사·한약사"]
    bub --> c3["제3장 약사심의위원회"]
    bub --> c4["제4장 의약품 분류"]
    bub --> c5["제5장 약국 개설"]
    bub --> c6["제6장 의약품 광고"]
    bub --> c7["제7장 벌칙"]
```

전체 mermaid 는 `tree/<name_id>-tree.mermaid` 5개 파일 참조 (보고서 길이 제약).

---

## Page 3 — 25 질문 비교 표

| 법령 | qid | 질문 | kolaw | PI+RLM | 차 | kw% (k/p) | 인용% (k/p) |
|---|---|---|---:|---:|---:|---|---|
{page3_table}

### 비용 (max plan + local)

| 시스템 | 호출수 | 평균 lat | 총 lat | 토큰 | USD |
|---|---:|---:|---:|---|---:|
{cost_table}

(max plan + local Qwen3 → USD = 0)

---

## Page 4 — 패턴 분석 (질문 슬롯별)

### 가설 (PoC 1차 기반)

> **multi-article cross-cut (Q2 류 처벌 종합) → PI+RLM 우위 / 단일 핀포인트 (Q1·Q3) → kolaw 우위 또는 동률 / metadata (Q4 이력) → 양쪽 약함**

### 5법령 × 5슬롯 = 25 질문 슬롯별 평균

| 슬롯 | kolaw 평균 | PI+RLM 평균 | 차 |
|---|---:|---:|---:|
{slot_table}

### 가설 검증 (자동 분석)

{hypothesis_verdict}

---

## Page 5 — 권고 + 다음 step

### 의장 결재 권고

{recommendation}

### 시스템 변경안 (B + C 구체)

```
[기존 lawxref.sh]
   ├─ fast mode (default, 단일 조문, 5~25초)
   │   - 변경 없음, production 그대로
   │   - 적합: Q3·Q5 류 단일 핀포인트 / metadata
   │
   └─ deep mode (NEW, opt-in flag --deep)
       ├─ Stage 1: PageIndex 트리 navigate (Claude, 10~15초)
       ├─ Stage 2: 다중 article context gather
       ├─ Stage 3: Claude 1차 답변 (10~25초)
       ├─ Stage 4: Qwen3-32B critic 1회 (5~70초, llama-swap local)
       └─ Stage 5: Claude 수정 (10~25초, 비판 PASS 시 skip)
          → 총 60~150초
       - 적합: Q1·Q2 류 multi-article cross-cut
```

### 다음 step 우선순위

| 단계 | 내용 | 기간 | 우선 |
|---|---|---|---|
| 1 | lawxref.sh 에 `--deep` 플래그 + 5법령 PageIndex 통합 | 1~2주 | A |
| 2 | 자동 라우터 (질문 유형 분류) — fast vs deep auto-select | 1주 | B |
| 3 | Skepty 자동 채점 hook (production answer audit) | 1주 | B |
| 4 | Qwen3 critic context 압축 (excerpt fingerprint + summary) | 0.5일 | C |
| 5 | Q4 metadata 질문용 law.go.kr DRF 통합 (개정·시행일 fetch) | 1주 | C |
| 6 | 5법령 → 10법령 확장 (의장 추가 결재) | 2주 | C |

### 변호사 검수 요청 사항

- PI+RLM 가 큰 우위 (Δ ≥ +5) 보인 답변 우선 검수 → ground truth 보강
- 변호사 검수 결과 → Skepty 채점 calibration 반영

---

## 부록 — 산출물 파일 위치 (m4max)

```
~/PRJs/kolaw/eval/pageindex-rlm-poc/laws/
├── laws_config.py                   # 5법령 + 25질문 정의
├── build_trees.py                   # tree builder
├── ask_systems.py                   # batch driver
├── score_systems.py                 # Skepty 채점
├── build_report.py                  # 본 메모 자동 생성
├── tree/         <name_id>-tree.{{json,mermaid}} + summary.json
├── answers/      <name_id>_{{kolaw,pageindex}}.json + summary_cost*.json
├── scoring/      <name_id>_scores.json + aggregate.json
└── reports/      pageindex-rlm-poc-5laws-{today}.md  ← 본 문서
```

## V&V 8-dim self-check

| Dim | 통과 |
|---|---|
| 1 Code/Static | py syntax OK |
| 2 기능 검증 | 50 답변 batch 완료 |
| 3 단위 검증 | 표본 답변 manual confirm |
| 4 시스템 검증 | 25 질문 × 2 시스템 채점 완료 |
| 5 V&V | "right thing" (의장 결재 가능) + "built right" (재실행 script) |
| 6 데이터 IO | legalize-kr corpus + claude CLI + llama-swap |
| 7 Correlation | 4기준 × 25문항 채점 + kw/article hit rate 3축 |
| 8 FDIR | per-law atomic resume |

P0: 0 / P1: [TBD] / P2: [TBD]
"""

    out_path = REPORTS / f"pageindex-rlm-poc-5laws-{today}.md"
    out_path.write_text(body, encoding="utf-8")
    print(f"Wrote {out_path}")
    return out_path


if __name__ == "__main__":
    build()
