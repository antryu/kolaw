"""
score_systems.py — Skepty 채점 (5 법령 × 5 질문 × 2 시스템 = 50 답변).

PoC1 score_answers.py 일반화. 4기준 × 1~10점 + keyword/article hit rate.
Skepty = Claude (Skepty 페르소나 prompt).

산출 (laws/scoring/):
- <name_id>_scores.json  # per-law table
- aggregate.json         # 5 law avg + per-question type breakdown
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ANSWERS = ROOT / "answers"
OUT = ROOT / "scoring"
OUT.mkdir(exist_ok=True, parents=True)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
from laws_config import LAWS, get_law  # noqa: E402
from llm_clients import call_claude  # noqa: E402


def score_one(name_id: str, q: dict, system: str, answer: str) -> dict:
    qid = q["id"]
    question = q["question"]
    expect_keywords = q.get("expect_keywords", [])
    ground_truth_articles = q.get("ground_truth_articles", [])

    keyword_hits = [k for k in expect_keywords if k in answer]
    # ground_truth_articles 형식: "민법 제527조" 또는 "민법 제527조 (청약)"
    article_hits = []
    for a in ground_truth_articles:
        # extract '제N조' or '제N조의M' token
        m = re.search(r"제\d+조(?:의\d+)?", a)
        token = m.group(0) if m else a
        if token in answer or a in answer:
            article_hits.append(a)

    display = get_law(name_id)["display"]
    prompt = f"""당신은 한국 법률 검토자 (Skepty) 입니다.
아래 답변을 4기준 × 1~10점으로 채점하고, 1줄 평가를 덧붙이세요.

[중요 채점 룰 — Day 4 calibration]
- 한국 법령은 자주 개정됩니다. 답변의 형량/기간이 본인의 학습 데이터와 다르더라도,
  **법령 개정으로 인한 최신 수치일 가능성**을 먼저 고려하세요.
- 예: 형법 §347 사기죄는 2025.12.23 개정으로 "10년/2천만원" → "20년/5천만원" 으로 상향됨.
- 즉 본인이 잘못된 옛 수치를 ground truth 라고 단정하지 말 것. 의심되면 **accuracy 감점 대신 comment 에 "검증 필요" 명시** 후 중간 점수.

[법령]
{display}

[질문]
{question}

[기대 키워드 — 정답에 포함되어야 할 단어]
{', '.join(expect_keywords)}

[ground truth 조문 — 답변에서 인용되어야 함]
{', '.join(ground_truth_articles) or '(none)'}

[채점 대상 답변 — 시스템: {system}]
{answer}

[채점 기준]
1. accuracy (정확도): {display} 실제 조문/사실과 일치 (1~10) — 옛 수치 가정 금지
2. logic (논리): 결론이 근거에서 도출 (1~10)
3. citation (인용): 조문 표기 정확, **명백한** hallucination 없음 (1~10)
4. conciseness (간결성): 핵심 답변, 군더더기 없음 (1~10)

[출력 — JSON only]
{{"accuracy": N, "logic": N, "citation": N, "conciseness": N, "comment": "한 줄"}}
"""
    rec = call_claude(prompt, max_tokens=400, role="skepty-score")
    parsed = {"accuracy": 0, "logic": 0, "citation": 0, "conciseness": 0, "comment": ""}
    if rec.text:
        m = re.search(r"\{.*?\}", rec.text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception as e:
                parsed["comment"] = f"parse error: {e} :: raw={rec.text[:200]}"
    parsed["sum"] = sum(parsed.get(k, 0) for k in ["accuracy", "logic", "citation", "conciseness"])
    parsed["keyword_hits"] = keyword_hits
    parsed["keyword_hit_rate"] = round(len(keyword_hits) / max(1, len(expect_keywords)), 2)
    parsed["article_hits"] = article_hits
    parsed["article_hit_rate"] = round(
        len(article_hits) / max(1, len(ground_truth_articles)) if ground_truth_articles else 0,
        2,
    )
    parsed["scorer_latency_ms"] = rec.latency_ms
    parsed["scorer_error"] = rec.error
    return parsed


def score_law(name_id: str) -> list[dict]:
    law = get_law(name_id)
    kolaw = json.loads((ANSWERS / f"{name_id}_kolaw.json").read_text(encoding="utf-8"))
    pi = json.loads((ANSWERS / f"{name_id}_pageindex.json").read_text(encoding="utf-8"))
    by_qid_kolaw = {r["qid"]: r for r in kolaw}
    by_qid_pi = {r["qid"]: r for r in pi}
    table: list[dict] = []
    for q in law["questions"]:
        qid = q["id"]
        a1 = (by_qid_kolaw.get(qid) or {}).get("answer", "(missing)")
        a2 = (by_qid_pi.get(qid) or {}).get("final_answer", "(missing)")
        print(f"  -> {name_id} {qid} kolaw...")
        s1 = score_one(name_id, q, "kolaw_baseline", a1)
        print(f"     sum={s1['sum']} kw={s1['keyword_hit_rate']} art={s1['article_hit_rate']}")
        print(f"  -> {name_id} {qid} pageindex...")
        s2 = score_one(name_id, q, "pageindex_rlm", a2)
        print(f"     sum={s2['sum']} kw={s2['keyword_hit_rate']} art={s2['article_hit_rate']}")
        table.append({
            "name_id": name_id,
            "qid": qid,
            "question": q["question"],
            "kolaw_baseline": s1,
            "pageindex_rlm": s2,
        })
    (OUT / f"{name_id}_scores.json").write_text(
        json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return table


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    full: list[dict] = []
    per_law_agg: dict[str, dict] = {}
    for law in LAWS:
        if target is not None and law["name_id"] != target:
            continue
        print(f"\n=== {law['name_id']} ({law['display']}) ===")
        table = score_law(law["name_id"])
        full.extend(table)
        n = len(table)
        per_law_agg[law["name_id"]] = {
            "display": law["display"],
            "n_questions": n,
            "kolaw_avg_sum": round(sum(t["kolaw_baseline"]["sum"] for t in table) / n, 2),
            "pi_avg_sum": round(sum(t["pageindex_rlm"]["sum"] for t in table) / n, 2),
            "kolaw_kw_avg": round(sum(t["kolaw_baseline"]["keyword_hit_rate"] for t in table) / n, 2),
            "pi_kw_avg": round(sum(t["pageindex_rlm"]["keyword_hit_rate"] for t in table) / n, 2),
            "kolaw_art_avg": round(sum(t["kolaw_baseline"]["article_hit_rate"] for t in table) / n, 2),
            "pi_art_avg": round(sum(t["pageindex_rlm"]["article_hit_rate"] for t in table) / n, 2),
            "delta_sum": round(
                sum(t["pageindex_rlm"]["sum"] - t["kolaw_baseline"]["sum"] for t in table) / n, 2
            ),
        }

    n = len(full) or 1
    grand = {
        "n_total": len(full),
        "kolaw_avg_sum": round(sum(t["kolaw_baseline"]["sum"] for t in full) / n, 2),
        "pi_avg_sum": round(sum(t["pageindex_rlm"]["sum"] for t in full) / n, 2),
        "kolaw_kw_avg": round(sum(t["kolaw_baseline"]["keyword_hit_rate"] for t in full) / n, 2),
        "pi_kw_avg": round(sum(t["pageindex_rlm"]["keyword_hit_rate"] for t in full) / n, 2),
        "kolaw_art_avg": round(sum(t["kolaw_baseline"]["article_hit_rate"] for t in full) / n, 2),
        "pi_art_avg": round(sum(t["pageindex_rlm"]["article_hit_rate"] for t in full) / n, 2),
        "delta_sum": round(
            sum(t["pageindex_rlm"]["sum"] - t["kolaw_baseline"]["sum"] for t in full) / n, 2
        ),
    }

    out = {
        "per_law": per_law_agg,
        "grand_avg": grand,
    }
    agg_path = OUT / ("aggregate.json" if target is None else f"aggregate_{target}.json")
    agg_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== Aggregate ===")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
