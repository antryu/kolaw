"""
run_one.py — 1개 질문 × 1개 시스템 단독 실행 + answers/ 에 incremental append.

usage:
  python run_one.py kolaw Q1
  python run_one.py pi Q3

이렇게 하면 batch fail 시 resume 가능.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ANSWERS = ROOT / "answers"
ANSWERS.mkdir(exist_ok=True)
QUESTIONS = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))


def append_result(path: Path, result: dict):
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    else:
        existing = []
    existing = [r for r in existing if r.get("qid") != result["qid"]]
    existing.append(result)
    existing.sort(key=lambda r: r["qid"])
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    if len(sys.argv) < 3:
        print("usage: run_one.py [kolaw|pi] [Q1..Q5]")
        sys.exit(1)
    system, qid = sys.argv[1], sys.argv[2]
    qs = {q["id"]: q for q in QUESTIONS["questions"]}
    if qid not in qs:
        print(f"unknown qid: {qid}")
        sys.exit(1)
    question = qs[qid]["question"]

    if system == "kolaw":
        from ask_kolaw import ask_kolaw_baseline
        r = ask_kolaw_baseline(qid, question)
        append_result(ANSWERS / "kolaw_baseline.json", r)
        print(f"OK kolaw {qid} sum_lat={r['latency_ms']}ms err={r['error']}")
    elif system == "pi":
        from ask_pageindex_rlm import ask_pageindex_rlm, load_tree
        tree = load_tree()
        r = ask_pageindex_rlm(qid, question, tree)
        append_result(ANSWERS / "pageindex_rlm.json", r)
        print(
            f"OK pi {qid} cycles={r['n_cycles']} sum_lat={r['latency_ms']}ms "
            f"in={r['input_tokens']} out={r['output_tokens']}"
        )
    else:
        print(f"unknown system: {system}")
        sys.exit(1)


if __name__ == "__main__":
    main()
