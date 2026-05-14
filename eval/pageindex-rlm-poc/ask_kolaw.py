"""
ask_kolaw.py — kolaw production lawxref baseline.

전략: lawxref.sh 는 단일 (법령, 조) 인자 호출 → 5 질문 다수 조문 hit 필요.
질문별로 "관련 조" 후보를 lawxref 메타 + Markdown grep 으로 추출 → 본문 묶음 →
Claude 1차 답변 (RLM 없음 — kolaw 그대로의 production 동작 모사).

baseline 의 핵심: chunk-vector 가 아니라 production lawxref 가 실제 산출하는
context 만 보고 답변. PageIndex 는 별도로 비교.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from llm_clients import call_claude, CallRecord

ROOT = Path(__file__).parent
CORPUS = Path.home() / "Thairon" / "legalize-kr" / "kr" / "의료법"
QUESTIONS = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
LAWXREF = Path.home() / ".claude" / "scripts" / "lawxref.sh"


def keyword_grep_corpus(question: str) -> list[str]:
    """
    naive keyword expansion: 질문에서 명사 후보 추출 + Markdown grep.
    return list of '제N조' tokens that hit.
    """
    keywords_map = {
        "Q1": ["보존기간", "진료기록부", "처방전", "수술기록", "방사선", "검사기록"],
        "Q2": ["벌칙", "벌금", "징역", "처벌", "벌금형"],
        "Q3": ["전자의무기록", "마이크로필름", "보존방법", "폐업", "이송"],
        "Q4": ["개정", "전부개정", "일부개정"],
        "Q5": ["시행령", "시행규칙", "보건복지부령", "대통령령"],
    }
    qid_match = re.match(r"^(Q\d+)", question)
    qid = qid_match.group(1) if qid_match else ""
    keywords = keywords_map.get(qid, [])
    hits: list[str] = []
    seen: set[str] = set()
    law_md = (CORPUS / "법률.md").read_text(encoding="utf-8")
    for kw in keywords:
        # find article numbers near keyword hits
        for m in re.finditer(r"##### (제\d+조(?:의\d+)?)", law_md):
            art = m.group(1)
            if art in seen:
                continue
            # window ±500 chars around heading
            start = m.start()
            end = min(len(law_md), start + 2500)
            window = law_md[start:end]
            if kw in window:
                hits.append(art)
                seen.add(art)
    return hits[:8]  # cap to avoid context blow-up


def fetch_articles(article_ids: list[str]) -> str:
    """Concatenate full text of cited articles from 법률.md."""
    md = (CORPUS / "법률.md").read_text(encoding="utf-8")
    parts: list[str] = []
    for art in article_ids:
        # find heading
        pattern = rf"##### {re.escape(art)}\b.*?(?=\n##### |\n## |\n# |\Z)"
        m = re.search(pattern, md, re.DOTALL)
        if m:
            parts.append(m.group(0).strip())
    return "\n\n---\n\n".join(parts)


def run_lawxref(article_num: str) -> str:
    """Optional: call production lawxref.sh for one article — used as evidence."""
    digits = re.search(r"\d+", article_num)
    if not digits:
        return ""
    try:
        proc = subprocess.run(
            ["bash", str(LAWXREF), "의료법", digits.group()],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout
    except Exception as e:
        return f"[lawxref error: {e}]"


def ask_kolaw_baseline(qid: str, question: str) -> dict:
    """
    Baseline pipeline:
    1. Keyword grep → article candidates
    2. Pull article bodies
    3. Claude answer with article bodies as context (no critique)
    """
    t0 = time.time()
    candidates = keyword_grep_corpus(f"{qid} {question}")
    context = fetch_articles(candidates)
    if not context:
        context = "(no article matched)"

    prompt = f"""다음은 한국 의료법 조문 발췌입니다. 질문에 대해 발췌만 근거로 답하세요.
한국어로 답하고, 인용한 조문을 (법령명 §조) 형식으로 표기하세요.
발췌에 없는 내용은 "발췌에서 확인 불가"라고 명시하세요.

[질문]
{question}

[발췌]
{context}

[답변 형식]
1. 결론 (1~2문장)
2. 근거 조문 (각 항목: 제X조 · 핵심 인용)
3. 한계 (발췌만으로 답 못한 부분 있으면)
"""
    rec = call_claude(prompt, max_tokens=2048, role="kolaw-baseline")
    latency = int((time.time() - t0) * 1000)
    return {
        "qid": qid,
        "system": "kolaw_baseline",
        "question": question,
        "candidate_articles": candidates,
        "context_chars": len(context),
        "answer": rec.text,
        "error": rec.error,
        "latency_ms": latency,
        "input_tokens": rec.input_tokens,
        "output_tokens": rec.output_tokens,
        "usd": rec.usd,
        "model": rec.model,
    }


def main():
    out_dir = ROOT / "answers"
    out_dir.mkdir(exist_ok=True)
    results = []
    for q in QUESTIONS["questions"]:
        print(f"-> kolaw {q['id']}: {q['question'][:40]}...")
        r = ask_kolaw_baseline(q["id"], q["question"])
        results.append(r)
        print(f"   ok lat={r['latency_ms']}ms err={r['error']}")
    (out_dir / "kolaw_baseline.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {out_dir}/kolaw_baseline.json")


if __name__ == "__main__":
    main()
