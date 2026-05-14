"""
run_batch.py — 5질문 × 2시스템 batch 실행 + cost ledger.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from ask_kolaw import ask_kolaw_baseline
from ask_pageindex_rlm import ask_pageindex_rlm, load_tree

ROOT = Path(__file__).parent
QUESTIONS = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))


def main():
    out_dir = ROOT / "answers"
    out_dir.mkdir(exist_ok=True)
    tree = load_tree()
    t0 = time.time()

    kolaw_results = []
    pageindex_results = []

    for q in QUESTIONS["questions"]:
        qid, question = q["id"], q["question"]
        print(f"\n=== {qid}: {question}")

        print(f"  [kolaw_baseline]")
        ts = time.time()
        r1 = ask_kolaw_baseline(qid, question)
        print(
            f"    candidates={len(r1['candidate_articles'])} "
            f"lat={r1['latency_ms']}ms err={r1['error']}"
        )
        kolaw_results.append(r1)

        print(f"  [pageindex+rlm]")
        ts = time.time()
        r2 = ask_pageindex_rlm(qid, question, tree)
        print(
            f"    nav={len(r2['navigated_nodes'])} cycles={r2['n_cycles']} "
            f"lat={r2['latency_ms']}ms in={r2['input_tokens']} out={r2['output_tokens']}"
        )
        pageindex_results.append(r2)

    (out_dir / "kolaw_baseline.json").write_text(
        json.dumps(kolaw_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "pageindex_rlm.json").write_text(
        json.dumps(pageindex_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # cost summary
    total_kolaw_in = sum(r["input_tokens"] for r in kolaw_results)
    total_kolaw_out = sum(r["output_tokens"] for r in kolaw_results)
    total_kolaw_lat = sum(r["latency_ms"] for r in kolaw_results)
    total_pi_in = sum(r["input_tokens"] for r in pageindex_results)
    total_pi_out = sum(r["output_tokens"] for r in pageindex_results)
    total_pi_lat = sum(r["latency_ms"] for r in pageindex_results)

    cost = {
        "kolaw_baseline": {
            "calls": len(kolaw_results),
            "input_tokens_proxy": total_kolaw_in,
            "output_tokens_proxy": total_kolaw_out,
            "latency_total_ms": total_kolaw_lat,
            "latency_avg_ms": total_kolaw_lat // max(1, len(kolaw_results)),
            "usd": 0.0,  # max plan
        },
        "pageindex_rlm": {
            "calls": len(pageindex_results),
            "claude_input_tokens_proxy": sum(
                cy["in_tokens"]
                for r in pageindex_results
                for cy in r["cycles"]
                if "claude" in cy["model"].lower()
            ),
            "claude_output_tokens_proxy": sum(
                cy["out_tokens"]
                for r in pageindex_results
                for cy in r["cycles"]
                if "claude" in cy["model"].lower()
            ),
            "qwen_input_tokens": sum(
                cy["in_tokens"]
                for r in pageindex_results
                for cy in r["cycles"]
                if "qwen" in cy["model"].lower()
            ),
            "qwen_output_tokens": sum(
                cy["out_tokens"]
                for r in pageindex_results
                for cy in r["cycles"]
                if "qwen" in cy["model"].lower()
            ),
            "total_cycles": sum(r["n_cycles"] for r in pageindex_results),
            "latency_total_ms": total_pi_lat,
            "latency_avg_ms": total_pi_lat // max(1, len(pageindex_results)),
            "usd": 0.0,  # max plan + local
        },
        "wall_time_s": round(time.time() - t0, 1),
    }
    (out_dir / "cost.json").write_text(
        json.dumps(cost, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n=== Cost summary ===")
    print(json.dumps(cost, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
