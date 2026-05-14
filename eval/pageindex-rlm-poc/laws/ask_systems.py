"""
ask_systems.py — 5 법령 × 5 질문 × 2 시스템 batch (R&D Track #2 Day 3~5).

PoC1 ask_kolaw.py / ask_pageindex_rlm.py 로직을 5법령으로 일반화.

전략:
- kolaw_baseline: 법령별 corpus md 에 question.expect_keywords 로 grep → article 후보 → claude 답변
- pageindex_rlm:  법령별 tree json 로드 → claude tree-nav → context → claude writer + qwen3 critic + cycle (≤3)

산출 (laws/answers/):
- <name_id>_kolaw.json
- <name_id>_pageindex.json
- batch_<name_id>.log
- summary_cost.json

PoC1 llm_clients (claude CLI + llama-swap qwen3) 그대로 재사용.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
TREE_DIR = ROOT / "tree"
OUT = ROOT / "answers"
OUT.mkdir(exist_ok=True, parents=True)

# allow `from laws_config import ...` and `from llm_clients import ...`
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))  # PoC1 llm_clients lives at parent
from laws_config import LAWS, sources_for, get_law  # noqa: E402
from llm_clients import call_claude, call_qwen, CallRecord  # noqa: E402

MAX_CYCLES = 3
ARTICLE_RE = re.compile(r"^##### (제\d+조(?:의\d+)?)", re.MULTILINE)


# -----------------------------------------------------------------------------
# kolaw baseline (keyword-grep RAG, no critique cycle)
# -----------------------------------------------------------------------------

def keyword_grep_corpus(name_id: str, question: dict) -> list[tuple[str, str]]:
    """
    Per-law: grep corpus markdown for article headings near expect_keywords.
    Returns list of (source_label, '제N조') hits, capped at 8.
    """
    keywords = question.get("expect_keywords", [])
    if not keywords:
        return []
    hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, path in sources_for(name_id):
        try:
            md = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in re.finditer(r"##### (제\d+조(?:의\d+)?)", md):
            art = m.group(1)
            key = (label, art)
            if key in seen:
                continue
            start = m.start()
            window = md[start : min(len(md), start + 2500)]
            if any(kw in window for kw in keywords):
                hits.append(key)
                seen.add(key)
    return hits[:8]


def fetch_articles(name_id: str, hits: list[tuple[str, str]]) -> str:
    """Concatenate full article bodies for the (source, article) pairs."""
    by_source: dict[str, str] = {}
    for label, path in sources_for(name_id):
        try:
            by_source[label] = path.read_text(encoding="utf-8")
        except Exception:
            pass
    parts: list[str] = []
    for label, art in hits:
        md = by_source.get(label, "")
        if not md:
            continue
        pat = rf"##### {re.escape(art)}\b.*?(?=\n##### |\n## |\n# |\Z)"
        m = re.search(pat, md, re.DOTALL)
        if m:
            parts.append(f"[{label} {art}]\n{m.group(0).strip()}")
    return "\n\n---\n\n".join(parts)


def ask_kolaw_baseline(name_id: str, question: dict) -> dict:
    t0 = time.time()
    qid = question["id"]
    q = question["question"]
    candidates = keyword_grep_corpus(name_id, question)
    context = fetch_articles(name_id, candidates)
    if not context:
        context = "(no article matched)"

    display = get_law(name_id)["display"]
    prompt = f"""다음은 한국 {display} 조문 발췌입니다. 발췌만 근거로 한국어로 답하세요.
인용은 (법령명 §조) 형식. 발췌에 없으면 "발췌에서 확인 불가" 명시.

[질문]
{q}

[발췌]
{context}

[답변 형식]
1. 결론 (1~2문장)
2. 근거 조문 (각 항목: 제X조 · 핵심 인용)
3. 한계
"""
    rec = call_claude(prompt, max_tokens=2048, role="kolaw-baseline")
    return {
        "name_id": name_id,
        "qid": qid,
        "system": "kolaw_baseline",
        "question": q,
        "candidate_articles": [f"{l}:{a}" for l, a in candidates],
        "context_chars": len(context),
        "answer": rec.text,
        "error": rec.error,
        "latency_ms": int((time.time() - t0) * 1000),
        "input_tokens": rec.input_tokens,
        "output_tokens": rec.output_tokens,
        "usd": rec.usd,
        "model": rec.model,
    }


# -----------------------------------------------------------------------------
# PageIndex + RLM (tree-nav + writer/critic cycle)
# -----------------------------------------------------------------------------

def load_tree(name_id: str) -> dict:
    p = TREE_DIR / f"{name_id}-tree.json"
    return json.loads(p.read_text(encoding="utf-8"))


def tree_summary(tree: dict, max_levels: int = 3) -> str:
    lines: list[str] = []

    def walk(n: dict, depth: int):
        if depth > max_levels:
            return
        prefix = "  " * depth
        marker = f"[{n.get('level_name','?')}]"
        lines.append(f"{prefix}{marker} {n.get('id','')} :: {n.get('title','')}")
        for c in n.get("children", []):
            walk(c, depth + 1)

    walk(tree, 0)
    return "\n".join(lines)


def find_node_by_id(tree: dict, target_id: str) -> dict | None:
    if tree.get("id") == target_id:
        return tree
    for c in tree.get("children", []):
        r = find_node_by_id(c, target_id)
        if r is not None:
            return r
    return None


def collect_articles_under(node: dict) -> list[dict]:
    out: list[dict] = []
    if node.get("level_name") == "article":
        out.append(node)
    for c in node.get("children", []):
        out.extend(collect_articles_under(c))
    return out


def pageindex_navigate(tree: dict, question: str, display: str) -> tuple[list[str], CallRecord]:
    outline = tree_summary(tree, max_levels=3)
    # 일부 법령 (자본시장법 등) outline 이 너무 길면 절단
    if len(outline) > 18000:
        outline = outline[:18000] + "\n... [outline truncated]"
    prompt = f"""당신은 한국 {display} 트리 탐색기입니다. 아래 outline 만 보고
질문에 답하기 위해 들여다봐야 할 노드 id 를 최대 3개까지 골라 JSON 으로 답하세요.
가능한 한 좁은 단계 (절·관·조) 노드를 고르세요.

질문: {question}

[Outline]
{outline}

[출력 형식 — JSON only, 다른 텍스트 X]
{{"node_ids": ["id1", "id2", "id3"], "reasoning": "한 줄"}}
"""
    rec = call_claude(prompt, max_tokens=400, role="pageindex-nav")
    ids: list[str] = []
    if rec.text:
        m = re.search(r"\{.*?\}", rec.text, re.DOTALL)
        if m:
            try:
                d = json.loads(m.group(0))
                ids = d.get("node_ids", [])[:3]
            except Exception:
                pass
    return ids, rec


def gather_context(tree: dict, node_ids: list[str], char_budget: int = 12000) -> str:
    chunks: list[str] = []
    used = 0
    for nid in node_ids:
        node = find_node_by_id(tree, nid)
        if node is None:
            continue
        articles = collect_articles_under(node)
        for art in articles:
            block = (
                f"[{nid} → {art.get('id','?')}] {art.get('title','')}\n"
                f"{art.get('body','')}"
            )
            if used + len(block) > char_budget:
                return "\n\n---\n\n".join(chunks) + "\n\n[... truncated]"
            chunks.append(block)
            used += len(block)
    return "\n\n---\n\n".join(chunks)


def writer_initial(question: str, context: str, display: str) -> CallRecord:
    prompt = f"""다음은 한국 {display} 트리 검색으로 추출된 조문입니다.
이 발췌만 근거로 질문에 한국어로 답하세요. 인용은 (법령명 §조) 형식.
발췌에 없으면 "발췌에서 확인 불가" 명시.

[질문]
{question}

[발췌]
{context}

[답변 형식]
1. 결론 (1~2문장)
2. 근거 조문 (각 항목: 제X조 · 핵심 인용)
3. 한계
"""
    return call_claude(prompt, max_tokens=2048, role="writer-initial")


def critic_critique(question: str, draft: str, context_excerpt: str) -> CallRecord:
    prompt = f"""당신은 한국 법률 검토자입니다. 아래 답변을 비판적으로 평가하세요.

[원 질문]
{question}

[참고 발췌의 도입부 — 답변이 이 범위 안인지 확인용]
{context_excerpt}

[답변]
{draft}

[검토 항목 — 각각 한 줄로]
1. 사실 오류 또는 인용 오류 있나? (어느 조문)
2. 발췌에 있는데 답변에서 빠진 핵심 정보 있나?
3. 발췌 밖 추정/할루시네이션 있나?
4. 답변이 질문에 직접 답하나?

[출력 형식 — 한국어, 8줄 이내, 마지막 줄에 'VERDICT: PASS' 또는 'VERDICT: REVISE']
"""
    return call_qwen(prompt, max_tokens=600, role="critique")


def writer_revise(question: str, prev_draft: str, critique: str, context: str) -> CallRecord:
    prompt = f"""아래는 직전 답변과 외부 비판자(다른 모델)의 비판입니다.
비판이 타당한 부분만 반영해 답변을 개선하세요. 발췌 범위 밖은 절대 추가 금지.

[질문]
{question}

[발췌]
{context}

[직전 답변]
{prev_draft}

[비판]
{critique}

[수정 답변 형식]
1. 결론
2. 근거 조문
3. 한계
4. (있으면) 비판 반영 요약 — 1~2줄
"""
    return call_claude(prompt, max_tokens=2048, role="writer-revise")


def is_fixed_point(prev: str, curr: str) -> bool:
    if not prev or not curr:
        return False
    a = prev.replace(" ", "").replace("\n", "")
    b = curr.replace(" ", "").replace("\n", "")
    if min(len(a), len(b)) < 100:
        return a == b
    ratio = min(len(a), len(b)) / max(len(a), len(b))
    if ratio < 0.85:
        return False
    common = sum(1 for x, y in zip(a, b) if x == y)
    return common / max(len(a), len(b)) > 0.8


def ask_pageindex_rlm(name_id: str, question: dict) -> dict:
    t0 = time.time()
    qid = question["id"]
    q = question["question"]
    display = get_law(name_id)["display"]
    tree = load_tree(name_id)

    nav_ids, nav_rec = pageindex_navigate(tree, q, display)
    context = gather_context(tree, nav_ids, char_budget=12000)
    if not context:
        context = "(no node matched — fallback empty)"

    cycles: list[dict] = []
    answer_rec = writer_initial(q, context, display)
    current_answer = answer_rec.text
    cycles.append({
        "cycle": 0,
        "role": "writer-initial",
        "model": answer_rec.model,
        "text": current_answer,
        "latency_ms": answer_rec.latency_ms,
        "in_tokens": answer_rec.input_tokens,
        "out_tokens": answer_rec.output_tokens,
        "usd": answer_rec.usd,
        "error": answer_rec.error,
    })

    for c in range(1, MAX_CYCLES + 1):
        critique_rec = critic_critique(q, current_answer, context[:1500])
        cycles.append({
            "cycle": c,
            "role": "critic",
            "model": critique_rec.model,
            "text": critique_rec.text,
            "latency_ms": critique_rec.latency_ms,
            "in_tokens": critique_rec.input_tokens,
            "out_tokens": critique_rec.output_tokens,
            "usd": critique_rec.usd,
            "error": critique_rec.error,
        })
        if "PASS" in (critique_rec.text or "").upper().split("VERDICT:")[-1]:
            break
        revise_rec = writer_revise(q, current_answer, critique_rec.text, context)
        new_answer = revise_rec.text
        cycles.append({
            "cycle": c,
            "role": "writer-revise",
            "model": revise_rec.model,
            "text": new_answer,
            "latency_ms": revise_rec.latency_ms,
            "in_tokens": revise_rec.input_tokens,
            "out_tokens": revise_rec.output_tokens,
            "usd": revise_rec.usd,
            "error": revise_rec.error,
        })
        if is_fixed_point(current_answer, new_answer):
            break
        current_answer = new_answer

    return {
        "name_id": name_id,
        "qid": qid,
        "system": "pageindex_rlm",
        "question": q,
        "navigated_nodes": nav_ids,
        "navigation_text": (nav_rec.text or nav_rec.error or "")[:300],
        "context_chars": len(context),
        "cycles": cycles,
        "final_answer": current_answer,
        "n_cycles": (len(cycles) - 1) // 2,
        "latency_ms": int((time.time() - t0) * 1000),
        "input_tokens": sum(x["in_tokens"] for x in cycles),
        "output_tokens": sum(x["out_tokens"] for x in cycles),
        "usd": round(sum(x["usd"] for x in cycles), 4),
    }


# -----------------------------------------------------------------------------
# Per-law batch driver
# -----------------------------------------------------------------------------

def run_law(name_id: str) -> dict:
    law = get_law(name_id)
    print(f"\n=== {name_id} ({law['display']}) ===")
    kolaw_results = []
    pi_results = []
    for q in law["questions"]:
        print(f"  -> {q['id']}: {q['question'][:50]}")
        print(f"     [kolaw_baseline]")
        ts = time.time()
        r1 = ask_kolaw_baseline(name_id, q)
        print(
            f"       cands={len(r1['candidate_articles'])} "
            f"lat={r1['latency_ms']}ms err={r1['error']}"
        )
        kolaw_results.append(r1)
        print(f"     [pageindex+rlm]")
        r2 = ask_pageindex_rlm(name_id, q)
        print(
            f"       nav={len(r2['navigated_nodes'])} cycles={r2['n_cycles']} "
            f"lat={r2['latency_ms']}ms in={r2['input_tokens']} out={r2['output_tokens']}"
        )
        pi_results.append(r2)

    (OUT / f"{name_id}_kolaw.json").write_text(
        json.dumps(kolaw_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / f"{name_id}_pageindex.json").write_text(
        json.dumps(pi_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "name_id": name_id,
        "kolaw_calls": len(kolaw_results),
        "kolaw_lat_ms": sum(r["latency_ms"] for r in kolaw_results),
        "pi_calls": len(pi_results),
        "pi_lat_ms": sum(r["latency_ms"] for r in pi_results),
        "pi_total_cycles": sum(r["n_cycles"] for r in pi_results),
        "pi_in_tokens": sum(r["input_tokens"] for r in pi_results),
        "pi_out_tokens": sum(r["output_tokens"] for r in pi_results),
    }


def main():
    t0 = time.time()
    target = sys.argv[1] if len(sys.argv) > 1 else None
    summary: list[dict] = []
    for law in LAWS:
        if target is not None and law["name_id"] != target:
            continue
        info = run_law(law["name_id"])
        summary.append(info)
    summary_path = OUT / ("summary_cost.json" if target is None else f"summary_cost_{target}.json")
    summary_path.write_text(
        json.dumps(
            {"per_law": summary, "wall_time_s": round(time.time() - t0, 1)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {summary_path} (wall {round(time.time() - t0, 1)}s)")


if __name__ == "__main__":
    main()
