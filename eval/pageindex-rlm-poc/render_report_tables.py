"""
render_report_tables.py — answers/ + scoring/ 결과를 5p 메모 Page 3·4 markdown 표로 렌더.

usage: python render_report_tables.py > /tmp/tables.md
그 후 보고서 파일에 [BATCH_RESULTS_TABLE] / [SCORING_DISTRIBUTION] 자리 치환.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent
ANSWERS = ROOT / "answers"
SCORING = ROOT / "scoring"


def render_q3_table():
    kolaw = json.loads((ANSWERS / "kolaw_baseline.json").read_text(encoding="utf-8"))
    pi = json.loads((ANSWERS / "pageindex_rlm.json").read_text(encoding="utf-8"))
    by_qid_k = {r["qid"]: r for r in kolaw}
    by_qid_p = {r["qid"]: r for r in pi}

    scores_path = SCORING / "scores.json"
    if scores_path.exists():
        scores = json.loads(scores_path.read_text(encoding="utf-8"))
        by_qid_s = {s["qid"]: s for s in scores}
    else:
        by_qid_s = {}

    print("| qid | 질문 | kolaw 합계 | PI+RLM 합계 | kolaw kw% | PI+RLM kw% | kolaw 인용% | PI+RLM 인용% | kolaw lat | PI+RLM lat | RLM cycles |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for qid in ("Q1", "Q2", "Q3", "Q4", "Q5"):
        k = by_qid_k.get(qid, {})
        p = by_qid_p.get(qid, {})
        s = by_qid_s.get(qid, {})
        sk = s.get("kolaw_baseline", {})
        sp = s.get("pageindex_rlm", {})
        question = k.get("question") or p.get("question") or ""
        if len(question) > 40:
            question = question[:40] + "…"
        print(
            f"| {qid} | {question} | "
            f"{sk.get('sum','-')} | {sp.get('sum','-')} | "
            f"{int((sk.get('keyword_hit_rate',0) or 0)*100)}% | "
            f"{int((sp.get('keyword_hit_rate',0) or 0)*100)}% | "
            f"{int((sk.get('article_hit_rate',0) or 0)*100)}% | "
            f"{int((sp.get('article_hit_rate',0) or 0)*100)}% | "
            f"{(k.get('latency_ms','-') or 0)//1000}s | "
            f"{(p.get('latency_ms','-') or 0)//1000}s | "
            f"{p.get('n_cycles','-')} |"
        )


def render_q4_findings():
    scores_path = SCORING / "scores.json"
    agg_path = SCORING / "aggregate.json"
    if not scores_path.exists() or not agg_path.exists():
        print("(scoring not yet run)")
        return
    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    agg = json.loads(agg_path.read_text(encoding="utf-8"))

    print("### 시스템별 평균 점수 (1~10, 합계 / 4항목 / 키워드·인용 적중)\n")
    print("| 시스템 | accuracy | logic | citation | conciseness | **합계 (40 만점)** | kw% | 인용% |")
    print("|---|---|---|---|---|---|---|---|")
    k = agg["kolaw_baseline_avg"]
    p = agg["pageindex_rlm_avg"]
    print(
        f"| kolaw_baseline | {k['accuracy']} | {k['logic']} | {k['citation']} | "
        f"{k['conciseness']} | **{k['sum']}** | {int(agg['kolaw_keyword_hit_avg']*100)}% | "
        f"{int(agg['kolaw_article_hit_avg']*100)}% |"
    )
    print(
        f"| pageindex_rlm | {p['accuracy']} | {p['logic']} | {p['citation']} | "
        f"{p['conciseness']} | **{p['sum']}** | {int(agg['pageindex_keyword_hit_avg']*100)}% | "
        f"{int(agg['pageindex_article_hit_avg']*100)}% |"
    )

    delta_sum = round(p["sum"] - k["sum"], 2)
    delta_kw = round((agg["pageindex_keyword_hit_avg"] - agg["kolaw_keyword_hit_avg"]) * 100, 1)
    delta_art = round((agg["pageindex_article_hit_avg"] - agg["kolaw_article_hit_avg"]) * 100, 1)
    sign = "+" if delta_sum >= 0 else ""
    print(f"\n**Δ (PI+RLM − kolaw)**: 합계 **{sign}{delta_sum}** 점, 키워드 **{delta_kw:+.1f}%p**, 인용 **{delta_art:+.1f}%p**\n")

    # per-question commentary
    print("### 질문별 Skepty 코멘트\n")
    for s in scores:
        qid = s["qid"]
        sk = s["kolaw_baseline"]
        sp = s["pageindex_rlm"]
        print(f"**{qid}** ({s['question']})")
        print(f"- kolaw ({sk['sum']}점): {sk.get('comment','')}")
        print(f"- PI+RLM ({sp['sum']}점): {sp.get('comment','')}\n")


if __name__ == "__main__":
    print("## Page 3 — 5 질문 비교 표\n")
    render_q3_table()
    print("\n\n## Page 4 — 채점 분포 + finding\n")
    render_q4_findings()
