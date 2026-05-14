"""
ask_pageindex_rlm.py — PageIndex(트리 reasoning) + RLM(자기비판 cycle) 답변.

PageIndex retrieve:
- chunk-vector 가 아니라, LLM 이 트리 nav (장 → 절 → 조) reasoning 으로 관련 노드 선정
- PoC 단순화: heuristic + LLM 1-step navigation (질문 → 장/절 후보 → 조 본문)

RLM cycle (Recursive Language Models):
- writer (Claude) 1차 답변 생성
- critic (Qwen3-32B llama-swap) 외부 비판 (family Byzantine)
- writer (Claude) 비판 반영 수정
- max 3 cycle, fixed-point 시 종료
- trace 보존

산출: answers/pageindex_rlm.json — 각 질문 당 cycles 전체 trace
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from llm_clients import call_claude, call_qwen, CallRecord

ROOT = Path(__file__).parent
TREE_JSON = ROOT / "tree" / "uirobub-tree.json"
QUESTIONS = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
MAX_CYCLES = 3


def load_tree() -> dict:
    return json.loads(TREE_JSON.read_text(encoding="utf-8"))


def tree_summary(tree: dict, max_levels: int = 3) -> str:
    """LLM 이 navigation 할 수 있도록 트리의 상위 N 레벨 outline 만 추출."""
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
    """주어진 노드 하위의 모든 article 노드 반환."""
    out: list[dict] = []
    if node.get("level_name") == "article":
        out.append(node)
    for c in node.get("children", []):
        out.extend(collect_articles_under(c))
    return out


def pageindex_navigate(tree: dict, question: str) -> tuple[list[str], CallRecord]:
    """
    LLM 1-step navigation: 트리 outline → 질문 관련 노드 id 후보 (장/절 단위) 선정.
    Claude 가 트리 outline 보고 어느 가지를 들여다볼지 결정.
    """
    outline = tree_summary(tree, max_levels=3)
    prompt = f"""당신은 한국 의료법 트리 탐색기입니다. 아래 outline 만 보고
질문에 답하기 위해 들여다봐야 할 노드 id 를 최대 3개까지 골라 JSON 으로 답하세요.

질문: {question}

[Outline]
{outline}

[출력 형식 — JSON only, 다른 텍스트 X]
{{"node_ids": ["id1", "id2", "id3"], "reasoning": "한 줄"}}
"""
    rec = call_claude(prompt, max_tokens=400, role="pageindex-nav")
    ids: list[str] = []
    if rec.text:
        # extract first JSON block
        m = re.search(r"\{.*?\}", rec.text, re.DOTALL)
        if m:
            try:
                d = json.loads(m.group(0))
                ids = d.get("node_ids", [])[:3]
            except Exception:
                pass
    return ids, rec


def gather_context(tree: dict, node_ids: list[str], char_budget: int = 12000) -> str:
    """선정된 노드들 하위의 article 본문을 모아 character budget 안에서 context 구성."""
    chunks: list[str] = []
    used = 0
    for nid in node_ids:
        node = find_node_by_id(tree, nid)
        if node is None:
            continue
        articles = collect_articles_under(node)
        for art in articles:
            block = f"[{nid} → {art.get('id','?')}] {art.get('title','')}\n{art.get('body','')}"
            if used + len(block) > char_budget:
                return "\n\n---\n\n".join(chunks) + "\n\n[... truncated]"
            chunks.append(block)
            used += len(block)
    return "\n\n---\n\n".join(chunks)


def writer_initial(question: str, context: str) -> CallRecord:
    prompt = f"""다음은 한국 의료법 트리 검색으로 추출된 조문입니다.
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
    """단순 fixed-point: 본문 80% 동일하면 수렴."""
    if not prev or not curr:
        return False
    a = prev.replace(" ", "").replace("\n", "")
    b = curr.replace(" ", "").replace("\n", "")
    if min(len(a), len(b)) < 100:
        return a == b
    # length-ratio + char-overlap proxy
    ratio = min(len(a), len(b)) / max(len(a), len(b))
    if ratio < 0.85:
        return False
    common = sum(1 for x, y in zip(a, b) if x == y)
    return common / max(len(a), len(b)) > 0.8


def ask_pageindex_rlm(qid: str, question: str, tree: dict) -> dict:
    t0 = time.time()
    nav_ids, nav_rec = pageindex_navigate(tree, question)
    context = gather_context(tree, nav_ids, char_budget=12000)
    if not context:
        context = "(no node matched — fallback to top-level law node)"

    cycles: list[dict] = []
    answer_rec = writer_initial(question, context)
    current_answer = answer_rec.text
    cycles.append(
        {
            "cycle": 0,
            "role": "writer-initial",
            "model": answer_rec.model,
            "text": current_answer,
            "latency_ms": answer_rec.latency_ms,
            "in_tokens": answer_rec.input_tokens,
            "out_tokens": answer_rec.output_tokens,
            "usd": answer_rec.usd,
            "error": answer_rec.error,
        }
    )

    for c in range(1, MAX_CYCLES + 1):
        critique_rec = critic_critique(question, current_answer, context[:1500])
        cycles.append(
            {
                "cycle": c,
                "role": "critic",
                "model": critique_rec.model,
                "text": critique_rec.text,
                "latency_ms": critique_rec.latency_ms,
                "in_tokens": critique_rec.input_tokens,
                "out_tokens": critique_rec.output_tokens,
                "usd": critique_rec.usd,
                "error": critique_rec.error,
            }
        )
        # PASS verdict 시 종료
        if "PASS" in (critique_rec.text or "").upper().split("VERDICT:")[-1]:
            break
        revise_rec = writer_revise(question, current_answer, critique_rec.text, context)
        new_answer = revise_rec.text
        cycles.append(
            {
                "cycle": c,
                "role": "writer-revise",
                "model": revise_rec.model,
                "text": new_answer,
                "latency_ms": revise_rec.latency_ms,
                "in_tokens": revise_rec.input_tokens,
                "out_tokens": revise_rec.output_tokens,
                "usd": revise_rec.usd,
                "error": revise_rec.error,
            }
        )
        if is_fixed_point(current_answer, new_answer):
            break
        current_answer = new_answer

    total_ms = int((time.time() - t0) * 1000)
    in_t = sum(x["in_tokens"] for x in cycles)
    out_t = sum(x["out_tokens"] for x in cycles)
    usd = round(sum(x["usd"] for x in cycles), 4)
    return {
        "qid": qid,
        "system": "pageindex_rlm",
        "question": question,
        "navigated_nodes": nav_ids,
        "navigation_text": nav_rec.text[:300] if nav_rec.text else nav_rec.error,
        "context_chars": len(context),
        "cycles": cycles,
        "final_answer": current_answer,
        "n_cycles": (len(cycles) - 1) // 2,
        "latency_ms": total_ms,
        "input_tokens": in_t,
        "output_tokens": out_t,
        "usd": usd,
    }


def main():
    out_dir = ROOT / "answers"
    out_dir.mkdir(exist_ok=True)
    tree = load_tree()
    results = []
    for q in QUESTIONS["questions"]:
        print(f"-> pageindex+rlm {q['id']}: {q['question'][:40]}...")
        r = ask_pageindex_rlm(q["id"], q["question"], tree)
        results.append(r)
        print(
            f"   ok cycles={r['n_cycles']} lat={r['latency_ms']}ms "
            f"in={r['input_tokens']} out={r['output_tokens']}"
        )
    (out_dir / "pageindex_rlm.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {out_dir}/pageindex_rlm.json")


if __name__ == "__main__":
    main()
