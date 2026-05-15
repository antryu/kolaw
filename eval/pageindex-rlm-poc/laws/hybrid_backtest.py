#!/usr/bin/env python3
"""
hybrid_backtest.py — critique-cycle hybrid (A+C) backtest
2026-05-15 의장 결재: ccv2 article 인용 메커니즘 → critique-cycle 통합

Production endpoint: engine=critique-cycle-v2 (= deep = runDeepPath)
hybrid A+C: PageIndex article_refs → writer prompt 에 제N조 명시 인용 강화

측정: answer, article_hit_rate (0.35 → 0.6+ 목표), 종합 sum (33.96 유지 목표)
결과: laws/scoring/hybrid_<name_id>.json + laws/scoring/hybrid_aggregate.json
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
SCORING_OUT = ROOT / "scoring"
SCORING_OUT.mkdir(exist_ok=True, parents=True)

YCMP_URL = "http://127.0.0.1:3888/api/comparison/runCell"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


def _load_anthropic_key() -> str:
    for env in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        v = __import__("os").environ.get(env, "")
        if v:
            return v
    plist = Path.home() / "Library" / "LaunchAgents" / "com.user.ytower.api.plist"
    try:
        text = plist.read_text(encoding="utf-8")
        m = re.search(r"CLAUDE_CODE_OAUTH_TOKEN.*?<string>(sk-ant-[^<]+)</string>", text, re.DOTALL)
        if m:
            return m.group(1)
        m2 = re.search(r"ANTHROPIC_API_KEY.*?<string>(sk-ant-[^<]+)</string>", text, re.DOTALL)
        if m2:
            return m2.group(1)
    except Exception:
        pass
    return ""


ANTHROPIC_KEY = _load_anthropic_key()


# 5법령 × 5Q (laws_config 동일 구조 재사용)
sys.path.insert(0, str(ROOT))
from laws_config import LAWS, get_law  # noqa: E402


def call_hybrid(law_name: str, question: str, timeout: int = 300) -> dict:
    """engine=critique-cycle-v2 (= deep hybrid A+C) 호출."""
    t0 = time.time()
    payload = json.dumps({
        "law_name": law_name,
        "question": question,
        "engine": "critique-cycle-v2",
    }).encode("utf-8")
    req = urllib.request.Request(
        YCMP_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        data["latency_ms"] = int((time.time() - t0) * 1000)
        return data
    except urllib.error.HTTPError as e:
        return {
            "ok": False,
            "error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}",
            "latency_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "latency_ms": int((time.time() - t0) * 1000),
        }


def _anthropic_complete(prompt: str, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 400) -> tuple[str, Optional[str]]:
    if not ANTHROPIC_KEY:
        return "", "no_api_key"
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
    }
    if ANTHROPIC_KEY.startswith("sk-ant-oat"):
        headers["authorization"] = f"Bearer {ANTHROPIC_KEY}"
        del headers["x-api-key"]
    req = urllib.request.Request(ANTHROPIC_API_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode("utf-8"))
        text = d.get("content", [{}])[0].get("text", "")
        return text, None
    except urllib.error.HTTPError as e:
        return "", f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        return "", f"{type(e).__name__}: {str(e)[:200]}"


def skepty_score(
    display: str, question: str, answer: str,
    expect_keywords: list[str], ground_truth_articles: list[str],
) -> dict:
    """ccv2_backtest.py 동일 Skepty 채점 조건."""
    if not answer or len(answer.strip()) < 20:
        return {
            "accuracy": 0, "logic": 0, "citation": 0, "conciseness": 0,
            "sum": 0, "comment": "answer empty/too short",
            "keyword_hit_rate": 0.0, "article_hit_rate": 0.0,
            "keyword_hits": [], "article_hits": [],
            "scorer_error": "empty_answer",
        }

    keyword_hits = [k for k in expect_keywords if k in answer]
    article_hits = []
    for a in ground_truth_articles:
        m = re.search(r"제\d+조(?:의\d+)?", a)
        token = m.group(0) if m else a
        if token in answer or a in answer:
            article_hits.append(a)

    prompt = f"""당신은 한국 법률 검토자 (Skepty) 입니다.
아래 답변을 4기준 × 1~10점으로 채점하고, 1줄 평가를 덧붙이세요.

[중요 채점 룰 — Day 4 calibration]
- 한국 법령은 자주 개정됩니다. 답변의 형량/기간이 본인의 학습 데이터와 다르더라도,
  법령 개정으로 인한 최신 수치일 가능성을 먼저 고려하세요.
- 예: 형법 §347 사기죄는 2025.12.23 개정으로 형량 상향됨.
- 즉 본인이 잘못된 옛 수치를 ground truth 라고 단정하지 말 것. 의심되면 accuracy 감점 대신
  comment 에 "검증 필요" 명시 후 중간 점수.

[법령]
{display}

[질문]
{question}

[기대 키워드 — 정답에 포함되어야 할 단어]
{', '.join(expect_keywords)}

[ground truth 조문 — 답변에서 인용되어야 함]
{', '.join(ground_truth_articles) or '(none)'}

[채점 대상 답변 — 시스템: hybrid-critique-cycle]
{answer}

[채점 기준]
1. accuracy (정확도): {display} 실제 조문/사실과 일치 (1~10)
2. logic (논리): 결론이 근거에서 도출 (1~10)
3. citation (인용): 조문 표기 정확, 명백한 hallucination 없음 (1~10)
4. conciseness (간결성): 핵심 답변, 군더더기 없음 (1~10)

[출력 — JSON only, 다른 텍스트 X]
{{"accuracy": N, "logic": N, "citation": N, "conciseness": N, "comment": "한 줄"}}
"""
    raw, err = _anthropic_complete(prompt)

    parsed = {"accuracy": 0, "logic": 0, "citation": 0, "conciseness": 0, "comment": ""}
    if raw:
        m = re.search(r"\{.*?\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception as e2:
                parsed["comment"] = f"parse error: {e2} :: {raw[:200]}"

    parsed["sum"] = sum(parsed.get(k, 0) for k in ["accuracy", "logic", "citation", "conciseness"])
    parsed["keyword_hits"] = keyword_hits
    parsed["keyword_hit_rate"] = round(len(keyword_hits) / max(1, len(expect_keywords)), 2)
    parsed["article_hits"] = article_hits
    parsed["article_hit_rate"] = round(
        len(article_hits) / max(1, len(ground_truth_articles)) if ground_truth_articles else 0, 2
    )
    parsed["scorer_error"] = err
    return parsed


def run_one(law: dict, q: dict) -> dict:
    display = law["display"]
    name_id = law["name_id"]
    qid = q["id"]
    question = q["question"]

    print(f"  [{name_id} {qid}] {question[:55]}...")
    t0 = time.time()
    resp = call_hybrid(display, question)
    wall_ms = int((time.time() - t0) * 1000)

    answer = resp.get("answer", "") or ""
    answer_source = "hybrid"
    deep_status = resp.get("deep_status", resp.get("error", "unknown"))

    # fallback 처리 (ccv2_backtest.py 동일)
    if not answer or deep_status == "error":
        pi_sub = resp.get("pi_subresult") or {}
        pi_ans = pi_sub.get("answer", "") or ""
        rag_sub = resp.get("rag_subresult") or {}
        rag_ans = rag_sub.get("answer", "") or ""

        if pi_ans and pi_sub.get("pi_status") == "ok":
            answer = pi_ans
            answer_source = "fallback_pi"
            deep_status = "critique_fail|pi_ok"
        elif rag_ans and rag_sub.get("kolaw_status") == "ok":
            answer = rag_ans
            answer_source = "fallback_rag"
            deep_status = "critique_fail|rag_ok"
        else:
            answer = rag_ans or pi_ans
            answer_source = "fallback_any"
            deep_status = "critique_fail|no_good_sub"

    citations = resp.get("citations", []) or []
    n_cycles = resp.get("n_cycles", -1)
    pipeline_passed = answer_source == "hybrid" and bool(answer) and resp.get("ok", False) and deep_status not in ("no_retrieval", "error")

    print(f"     -> ok={resp.get('ok')} n_cycles={n_cycles} status={deep_status} src={answer_source} lat={wall_ms}ms")

    print(f"     [Skepty scoring...]")
    scores = skepty_score(
        display, question, answer,
        q["expect_keywords"], q["ground_truth_articles"],
    )
    print(f"     -> sum={scores['sum']}/40 kw={scores['keyword_hit_rate']} art={scores['article_hit_rate']}")

    return {
        "name_id": name_id,
        "display": display,
        "qid": qid,
        "question": question,
        "hybrid_response": {
            "ok": resp.get("ok"),
            "engine": resp.get("engine"),
            "n_cycles": n_cycles,
            "deep_status": deep_status,
            "answer_source": answer_source,
            "n_citations": len(citations),
            "latency_ms": wall_ms,
            "error": resp.get("error"),
        },
        "answer": answer,
        "pipeline_passed": pipeline_passed,
        "scores": scores,
    }


def run_law(law: dict) -> list[dict]:
    name_id = law["name_id"]
    out_path = SCORING_OUT / f"hybrid_{name_id}.json"

    done_qids: set[str] = set()
    results: list[dict] = []
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            results = prev
            done_qids = {r["qid"] for r in results}
            print(f"  [resume] {name_id}: {len(done_qids)} questions already done")
        except Exception:
            pass

    for q in law["questions"]:
        if q["id"] in done_qids:
            print(f"  [skip] {name_id} {q['id']}")
            continue
        rec = run_one(law, q)
        results.append(rec)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    return results


def aggregate(all_results: list[dict]) -> dict:
    per_law: dict[str, dict] = {}
    for r in all_results:
        nid = r["name_id"]
        if nid not in per_law:
            per_law[nid] = {
                "display": r["display"],
                "n_questions": 0,
                "sums": [], "kw_rates": [], "art_rates": [],
                "pipeline_passed": 0, "latencies_ms": [],
            }
        g = per_law[nid]
        g["n_questions"] += 1
        g["sums"].append(r["scores"].get("sum", 0))
        g["kw_rates"].append(r["scores"].get("keyword_hit_rate", 0.0))
        g["art_rates"].append(r["scores"].get("article_hit_rate", 0.0))
        if r.get("pipeline_passed"):
            g["pipeline_passed"] += 1
        g["latencies_ms"].append(r["hybrid_response"].get("latency_ms", 0))

    per_law_agg: dict[str, dict] = {}
    for nid, g in per_law.items():
        n = g["n_questions"] or 1
        per_law_agg[nid] = {
            "display": g["display"],
            "n_questions": g["n_questions"],
            "hybrid_mean_sum": round(sum(g["sums"]) / n, 2),
            "hybrid_kw_avg": round(sum(g["kw_rates"]) / n, 2),
            "hybrid_art_avg": round(sum(g["art_rates"]) / n, 2),
            "pipeline_pass_rate": round(g["pipeline_passed"] / n, 2),
            "mean_latency_ms": round(sum(g["latencies_ms"]) / n),
        }

    total_sums = [r["scores"].get("sum", 0) for r in all_results]
    total_kw = [r["scores"].get("keyword_hit_rate", 0.0) for r in all_results]
    total_art = [r["scores"].get("article_hit_rate", 0.0) for r in all_results]
    total_passed = sum(1 for r in all_results if r.get("pipeline_passed"))
    total_lat = [r["hybrid_response"].get("latency_ms", 0) for r in all_results]
    n = len(all_results) or 1

    grand = {
        "n_total": len(all_results),
        "hybrid_mean_sum": round(sum(total_sums) / n, 2),
        "hybrid_median_sum": round(sorted(total_sums)[n // 2], 2),
        "hybrid_kw_avg": round(sum(total_kw) / n, 2),
        "hybrid_art_avg": round(sum(total_art) / n, 2),
        "pipeline_pass_rate": round(total_passed / n, 2),
        "mean_latency_ms": round(sum(total_lat) / n),
        "baseline_pi_art_avg": 0.35,  # pre-hybrid baseline
        "baseline_pi_mean_sum": 33.96,  # pre-hybrid baseline (의장 mandate)
        "all_sums": total_sums,
    }

    return {"per_law": per_law_agg, "grand": grand}


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    t_wall = time.time()
    all_results: list[dict] = []

    for law in LAWS:
        if target and law["name_id"] != target:
            continue
        print(f"\n=== {law['name_id']} ({law['display']}) ===")
        results = run_law(law)
        all_results.extend(results)

    agg = aggregate(all_results)

    agg_path = SCORING_OUT / ("hybrid_aggregate.json" if not target else f"hybrid_aggregate_{target}.json")
    agg_path.write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== Hybrid 25Q Aggregate (wall {round(time.time()-t_wall,1)}s) ===")
    print(json.dumps(agg, ensure_ascii=False, indent=2))
    print(f"\nWrote: {agg_path}")

    # 회귀 체크
    g = agg["grand"]
    art = g["hybrid_art_avg"]
    s = g["hybrid_mean_sum"]
    print(f"\n[회귀 체크]")
    print(f"  article_hit_rate: {art:.2f} (목표 >= 0.6, baseline 0.35) — {'PASS' if art >= 0.6 else 'BELOW_TARGET'}")
    print(f"  mean_sum: {s:.2f} (목표 >= 33.96 유지) — {'PASS' if s >= 30 else 'REGRESSION_RISK'}")


if __name__ == "__main__":
    main()
