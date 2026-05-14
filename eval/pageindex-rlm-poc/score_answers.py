"""
score_answers.py — Skepty (정합성 채점) — Claude 가 두 시스템 답변을 1~10점으로 채점.

채점 기준:
- accuracy (정확도): 의료법 실제 조문 사실과 일치하는가
- logic (논리): 결론이 근거에서 도출되나
- citation (인용 매핑): (법령명 §조) 정확한가, hallucination 없나
- conciseness (간결성): 군더더기 없이 핵심 답하나

ground_truth: questions.json 의 expect_keywords + ground_truth_articles
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from llm_clients import call_claude

ROOT = Path(__file__).parent
QUESTIONS = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))


def score_one(qid: str, question: str, expect_keywords: list[str],
              ground_truth_articles: list[str], system: str, answer: str) -> dict:
    keyword_hits = [k for k in expect_keywords if k in answer]
    article_hits = [a for a in ground_truth_articles if a.split(" ")[1] in answer or a in answer]

    prompt = f"""당신은 한국 법률 검토자 (Skepty) 입니다.
아래 답변을 4기준 × 1~10점으로 채점하고, 1줄 평가를 덧붙이세요.

[질문]
{question}

[기대 키워드 — 정답에 포함되어야 할 단어]
{', '.join(expect_keywords)}

[ground truth 조문 — 답변에서 인용되어야 함]
{', '.join(ground_truth_articles) or '(none)'}

[채점 대상 답변 — 시스템: {system}]
{answer}

[채점 기준]
1. accuracy (정확도): 의료법 실제 조문/사실과 일치 (1~10)
2. logic (논리): 결론이 근거에서 도출 (1~10)
3. citation (인용): 조문 표기 정확, hallucination 없음 (1~10)
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
    parsed["article_hit_rate"] = round(len(article_hits) / max(1, len(ground_truth_articles)) if ground_truth_articles else 0, 2)
    parsed["scorer_latency_ms"] = rec.latency_ms
    parsed["scorer_error"] = rec.error
    return parsed


def main():
    answers_dir = ROOT / "answers"
    out_dir = ROOT / "scoring"
    out_dir.mkdir(exist_ok=True)

    kolaw = json.loads((answers_dir / "kolaw_baseline.json").read_text(encoding="utf-8"))
    pageindex = json.loads((answers_dir / "pageindex_rlm.json").read_text(encoding="utf-8"))

    by_qid_kolaw = {r["qid"]: r for r in kolaw}
    by_qid_pi = {r["qid"]: r for r in pageindex}

    table: list[dict] = []
    for q in QUESTIONS["questions"]:
        qid, question = q["id"], q["question"]
        exk = q.get("expect_keywords", [])
        gta = q.get("ground_truth_articles", [])

        a1 = (by_qid_kolaw.get(qid) or {}).get("answer", "(missing)")
        a2 = (by_qid_pi.get(qid) or {}).get("final_answer", "(missing)")
        print(f"-> scoring {qid} kolaw...")
        s1 = score_one(qid, question, exk, gta, "kolaw_baseline", a1)
        print(f"   sum={s1['sum']} kw_hits={s1['keyword_hit_rate']}")
        print(f"-> scoring {qid} pageindex_rlm...")
        s2 = score_one(qid, question, exk, gta, "pageindex_rlm", a2)
        print(f"   sum={s2['sum']} kw_hits={s2['keyword_hit_rate']}")
        table.append(
            {
                "qid": qid,
                "question": question,
                "kolaw_baseline": s1,
                "pageindex_rlm": s2,
            }
        )

    (out_dir / "scores.json").write_text(
        json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # aggregate
    n = len(table)
    agg = {
        "n_questions": n,
        "kolaw_baseline_avg": {
            k: round(sum(t["kolaw_baseline"][k] for t in table) / n, 2)
            for k in ["accuracy", "logic", "citation", "conciseness", "sum"]
        },
        "pageindex_rlm_avg": {
            k: round(sum(t["pageindex_rlm"][k] for t in table) / n, 2)
            for k in ["accuracy", "logic", "citation", "conciseness", "sum"]
        },
        "kolaw_keyword_hit_avg": round(
            sum(t["kolaw_baseline"]["keyword_hit_rate"] for t in table) / n, 2
        ),
        "pageindex_keyword_hit_avg": round(
            sum(t["pageindex_rlm"]["keyword_hit_rate"] for t in table) / n, 2
        ),
        "kolaw_article_hit_avg": round(
            sum(t["kolaw_baseline"]["article_hit_rate"] for t in table) / n, 2
        ),
        "pageindex_article_hit_avg": round(
            sum(t["pageindex_rlm"]["article_hit_rate"] for t in table) / n, 2
        ),
    }
    (out_dir / "aggregate.json").write_text(
        json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== Aggregate ===")
    print(json.dumps(agg, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
