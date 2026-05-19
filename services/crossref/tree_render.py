"""
위임 체인 트리 렌더 (Phase 3).

Phase 2 의 ``DelegationChain`` (본법 조문 → 시행령/시행규칙 조문 → 별표 위임
관계) 을 사람이 한눈에 읽는 **들여쓰기 계층 텍스트** 로 변환한다.

의장 요구: "평평한 링크 목록 금지, 위임 체인 트리로 한눈에."

설계 메모
---------
* 입력은 ``lookup._normalize_chain`` 이 만든 dict 그대로 — Pydantic
  ``DelegationChain`` 으로 감싸기 전/후 어느 쪽이든 받도록 ``.get`` 만 사용.
* 출력은 순수 텍스트 트리. mermaid·HTML 금지 — Discord 등 미렌더 환경에서
  깨지지 않게 한다.
* 계층: 본법 조문 → (위임 종류) → 시행령 조문 → 시행규칙 조문(시행령 아래
  중첩 시) / 본법 직속 시행규칙 조문 → 별표.
* hit 마커: 검색·조회로 직접 hit 된 조문은 ``▶`` 로 표시한다. 호출자가
  hit 된 조문의 doc_id 를 ``hit_doc_id`` 로 넘기면 그 줄 앞에 마커가 붙는다.
  미지정이면 마커 없음(회귀 없음).
* 별표 줄: 별표 본문(Phase 3 A안)이 체인에 붙어 있으면 ``(본문 있음)`` /
  ``(이미지형, 본문 없음)`` 만 표시한다 — 본문 텍스트·표는 트리에 도배하지
  않고 체인 dict 의 ``byeolpyo[].bodies`` 구조화 필드에 둔다(가독성 유지).
"""

from __future__ import annotations

# 들여쓰기 한 단계 — 본법(0) / 시행령·본법직속시행규칙(1) / 중첩시행규칙(2).
_INDENT = "  "
# 위임/참조 가지 접두.
_BRANCH = "└─"
# hit 된 조문 마커 (호출자가 hit_doc_id 를 줄 때만 사용).
_HIT = "▶ "
_NOHIT = "  "


def _article_label(article: str, title: str) -> str:
    """'제29조의8 (개인정보의 국외 이전 인증)' 형태로 조문 라벨 조립."""
    article = (article or "").strip()
    title = (title or "").strip()
    if article and title:
        return f"{article} {title}"
    return article or title


def _kind_label(delegation_kind: list[str] | None) -> str:
    """위임 종류 라벨 — '대통령령' / '총리령·부령' 등. 비었으면 빈 문자열."""
    kinds = [k for k in (delegation_kind or []) if k and k != "별표"]
    return "·".join(kinds)


def resolve_hit_doc_id(
    chain: dict | None,
    file_type: str | None,
    article: str | None,
) -> str | None:
    """
    체인 안에서 (file_type, article) 에 해당하는 조문의 doc_id 를 찾는다.

    ``/article`` 처럼 doc_id 가 아니라 (법령종류, 조문) 쌍만 쥔 호출자가
    ``render_delegation_tree`` 의 ``hit_doc_id`` 인자를 채울 때 쓴다.
    체인에 일치하는 조문이 없으면 None.
    """
    if not chain or not (file_type and article):
        return None
    if hasattr(chain, "model_dump"):
        chain = chain.model_dump()

    # 본법 조문.
    if file_type == "법률" and chain.get("law_article") == article:
        return chain.get("law_doc_id") or None

    # 시행령 조문 + 그 아래 중첩 시행규칙.
    for decree in chain.get("decree_articles", []) or []:
        if (
            decree.get("file_type", "시행령") == file_type
            and decree.get("article") == article
        ):
            return decree.get("doc_id") or None
        for sub_rule in decree.get("rule_articles", []) or []:
            if (
                sub_rule.get("file_type", "시행규칙") == file_type
                and sub_rule.get("article") == article
            ):
                return sub_rule.get("doc_id") or None

    # 본법 직속 시행규칙 조문.
    for rule in chain.get("rule_articles", []) or []:
        if (
            rule.get("file_type", "시행규칙") == file_type
            and rule.get("article") == article
        ):
            return rule.get("doc_id") or None

    return None


def render_delegation_tree(
    chain: dict | None,
    hit_doc_id: str | None = None,
) -> str:
    """
    ``DelegationChain`` dict 를 들여쓰기 트리 텍스트로 렌더.

    Parameters
    ----------
    chain:
        ``lookup._normalize_chain`` 형식 dict (또는 동일 필드의 Pydantic
        모델을 ``.model_dump()`` 한 dict). None 이면 빈 문자열 반환.
    hit_doc_id:
        검색·조회로 직접 hit 된 조문의 ChromaDB doc_id. 트리에서 그 조문
        줄 앞에 ``▶`` 마커를 붙인다. None 이면 마커 없음.

    Returns
    -------
    순수 텍스트 트리. None/빈 체인이면 "".
    """
    if not chain:
        return ""

    # dict 가 아니라 Pydantic 모델이 넘어와도 견디게.
    if hasattr(chain, "model_dump"):
        chain = chain.model_dump()

    lines: list[str] = []
    mark_used = hit_doc_id is not None

    def _prefix(doc_id: str | None) -> str:
        """hit 마커 접두 — hit_doc_id 미지정 시 빈 문자열."""
        if not mark_used:
            return ""
        return _HIT if (doc_id and doc_id == hit_doc_id) else _NOHIT

    # --- 본법 조문 (루트) ---------------------------------------------------
    law_doc_id = chain.get("law_doc_id", "")
    law_label = _article_label(
        chain.get("law_article", ""), chain.get("law_title", "")
    )
    law_name = (chain.get("law_name", "") or "").strip()
    root_text = f"{law_name} {law_label}".strip() if law_name else law_label
    lines.append(f"{_prefix(law_doc_id)}{root_text}")

    kind = _kind_label(chain.get("delegation_kind"))
    # 시행령 가지에 붙일 위임 라벨 — 종류가 있으면 '위임(대통령령)', 없으면 '위임'.
    decree_via = f"위임({kind})" if kind else "위임"

    # --- 시행령 조문들 + 그 아래 중첩 시행규칙 ------------------------------
    for decree in chain.get("decree_articles", []) or []:
        d_doc = decree.get("doc_id", "")
        d_label = _article_label(decree.get("article", ""), decree.get("title", ""))
        d_ftype = decree.get("file_type", "시행령")
        lines.append(
            f"{_prefix(d_doc)}{_INDENT}{_BRANCH}{decree_via}→ {d_ftype} {d_label}"
        )
        # 시행령 조문 아래 중첩된 시행규칙 (build_index 가 있을 때만 넣는 키).
        for sub_rule in decree.get("rule_articles", []) or []:
            sr_doc = sub_rule.get("doc_id", "")
            sr_label = _article_label(
                sub_rule.get("article", ""), sub_rule.get("title", "")
            )
            sr_ftype = sub_rule.get("file_type", "시행규칙")
            lines.append(
                f"{_prefix(sr_doc)}{_INDENT}{_INDENT}{_BRANCH}위임→ "
                f"{sr_ftype} {sr_label}"
            )

    # --- 본법 직속 시행규칙 조문들 ------------------------------------------
    for rule in chain.get("rule_articles", []) or []:
        r_doc = rule.get("doc_id", "")
        r_label = _article_label(rule.get("article", ""), rule.get("title", ""))
        r_ftype = rule.get("file_type", "시행규칙")
        lines.append(
            f"{_prefix(r_doc)}{_INDENT}{_BRANCH}위임(총리령·부령)→ "
            f"{r_ftype} {r_label}"
        )

    # --- 별표 (doc_id 없음 — 코퍼스 미수록) ---------------------------------
    # Phase 3 A안: byeolpyo 항목은 enrich 된 dict
    # ({별표, body_available, bodies}) 또는 (미enrich 시) 별표 번호 문자열.
    # 트리 가독성을 위해 본문 자체는 싣지 않고 가용 여부만 표시한다 —
    # 실제 본문(text·tables)은 체인 dict 의 byeolpyo[].bodies 에 있다.
    for bp in chain.get("byeolpyo", []) or []:
        if isinstance(bp, dict):
            bp_label = str(bp.get("별표", "")).strip()
            if not bp_label:
                continue
            bodies = bp.get("bodies", []) or []
            if bp.get("body_available"):
                avail = " (본문 있음)"
            elif bodies and all(b.get("is_image") for b in bodies):
                avail = " (이미지형, 본문 없음)"
            else:
                avail = ""
        else:
            bp_label = str(bp).strip()
            if not bp_label:
                continue
            avail = ""
        lines.append(
            f"{_prefix(None)}{_INDENT}{_BRANCH}참조→ 별표 {bp_label}{avail}"
        )

    return "\n".join(lines)
