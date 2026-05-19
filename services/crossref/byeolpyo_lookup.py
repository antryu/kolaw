"""
별표(別表) 본문 lookup — Phase 3 A안 마지막 청크.

``services/crossref/byeolpyo_body.py`` 가 별표 본문을 추출해 사이드카
``services/crossref/byeolpyo_bodies/<법령명_folded>.json`` 710개로 저장했다.
이 모듈은 그 본문을 위임 체인의 별표 참조에 O(1) 로 붙여 준다.

데이터 경로
-----------
* 사이드카 파일명 = ``법령명_folded`` (공백 제거한 법령명). 색인
  (``services/crossref/index/<...>.json``) 의 ``law_name`` 을 공백 제거하면
  사이드카 파일명과 일치한다 — 검증 결과 710/710 join.
* 사이드카 한 건 스키마: ``{schema_version, 법령명, 법령명_folded, scope,
  byeolpyo_count, byeolpyo:[...]}``. ``byeolpyo`` 항목 스키마는
  ``{별표, 별표번호, 별표명, 관련법령명, attached_article, 별표일련번호,
  pdf_url, hwp_url, is_image, page_count, image_count, text,
  tables:[{page, markdown}], error}``.
* 위임 체인(``lookup._normalize_chain``)의 ``byeolpyo`` 필드는 별표 *번호*
  문자열 리스트(예: ``["1", "2"]``). 이 번호로 사이드카의 ``별표`` 필드를
  매칭한다.

설계 메모
---------
* **lazy-per-law 로딩.** 30 MB / 710 파일 — 전부 메모리에 올려도 부담은
  적지만, 별표 본문이 없는 법령(570개)까지 읽을 이유가 없고 text/tables 가
  큰 법령(최대 895 KB)도 있어 첫 조회 시 해당 법령 파일만 읽어 캐시한다.
  법령 수가 710 으로 유계라 캐시 eviction 은 불필요.
* 별표 번호는 한 법령 안에서 시행령·시행규칙으로 갈려 중복될 수 있다
  (710개 중 246개 파일에서 동일 번호 충돌). 따라서 (법령, 별표번호) → 본문은
  **1:N** — 매칭되는 본문을 전부 리스트로 돌려준다.
* ``is_image=true`` 별표는 OCR 미적용이라 ``text`` 가 비어 있다 — 본문 대신
  ``image=true`` 플래그만 전달한다.
* crossref 색인을 절대 깨면 안 된다: 파일 없음·파싱 실패는 조용히 빈 결과로
  처리하고 호출자는 회귀 없이 동작한다.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# services/crossref/byeolpyo_bodies/ — byeolpyo_body.py 가 만든 사이드카 dir.
_BODIES_DIR = Path(__file__).resolve().parent / "byeolpyo_bodies"

# 법령명_folded -> {별표번호 -> [body dict, ...]}. 첫 조회 시 해당 법령 파일만
# 읽어 채운다. 값이 None 이면 "사이드카 없음/로드 실패" — 재시도하지 않는다.
_BODIES_BY_LAW: dict[str, dict[str, list[dict]] | None] = {}

_LOAD_LOCK = threading.Lock()


def _fold(law_name: str | None) -> str:
    """법령명 → 사이드카 파일명 키 (공백 제거)."""
    return (law_name or "").replace(" ", "")


def _load_law_bodies(folded: str) -> dict[str, list[dict]] | None:
    """
    한 법령의 별표 본문 사이드카를 읽어 ``별표번호 -> [body, ...]`` 로 만든다.

    파일이 없거나 파싱이 실패하면 None — 호출자는 "별표 본문 없음" 으로 본다.
    """
    path = _BODIES_DIR / f"{folded}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("byeolpyo bodies skip %s: %s", path.name, exc)
        return None
    by_num: dict[str, list[dict]] = {}
    for item in data.get("byeolpyo", []) or []:
        raw = item.get("별표")
        # licbyl 데이터에 별표 번호가 비어 있는 항목(별표 None)이 드물게 있다 —
        # 체인의 byeolpyo 번호 리스트로는 매칭될 수 없으니 색인에서 제외한다.
        num = str(raw).strip() if raw is not None else ""
        if num:
            by_num.setdefault(num, []).append(item)
    return by_num


def _get_law_bodies(law_name: str | None) -> dict[str, list[dict]] | None:
    """한 법령의 ``별표번호 -> [body,...]`` 맵을 lazy 로 얻어 캐시한다."""
    folded = _fold(law_name)
    if not folded:
        return None
    if folded not in _BODIES_BY_LAW:
        with _LOAD_LOCK:
            if folded not in _BODIES_BY_LAW:
                _BODIES_BY_LAW[folded] = _load_law_bodies(folded)
    return _BODIES_BY_LAW[folded]


def _body_summary(item: dict) -> dict:
    """
    사이드카 별표 항목 한 건을 체인에 실을 요약 dict 로 정규화.

    ``is_image=true`` 별표는 ``text``/``tables`` 가 비어 있으므로 ``image``
    플래그만 세운다. 그 외엔 본문(``text`` + 표 markdown)을 그대로 싣는다.
    """
    is_image = bool(item.get("is_image"))
    summary: dict = {
        "별표": str(item.get("별표", "")).strip(),
        "별표명": item.get("별표명", "") or "",
        "관련법령명": item.get("관련법령명", "") or "",
        "별표일련번호": item.get("별표일련번호", "") or "",
        "attached_article": item.get("attached_article", "") or "",
        "is_image": is_image,
        "pdf_url": item.get("pdf_url", "") or "",
    }
    if is_image:
        # 이미지형 별표 — OCR 미적용, 본문 없음.
        summary["body_available"] = False
        summary["note"] = "이미지형(OCR 미적용)"
        summary["text"] = ""
        summary["tables"] = []
    else:
        text = item.get("text", "") or ""
        tables = item.get("tables", []) or []
        summary["body_available"] = bool(text or tables)
        summary["text"] = text
        summary["tables"] = tables
    return summary


def enrich_byeolpyo_refs(
    law_name: str | None,
    byeolpyo_refs: list,
) -> list:
    """
    위임 체인의 별표 참조 리스트에 별표 본문을 붙인다.

    Parameters
    ----------
    law_name:
        체인이 속한 법령명 (``lookup._normalize_chain`` 이 체인에 실어 둠).
    byeolpyo_refs:
        체인의 ``byeolpyo`` 필드 — 별표 번호 문자열 리스트 (예: ``["1","2"]``).
        하위호환을 위해 이미 dict 인 항목도 그대로 통과시킨다.

    Returns
    -------
    각 항목이 다음 형태인 dict 리스트::

        {별표, body_available, bodies:[{별표명, 관련법령명, 별표일련번호,
         is_image, body_available, text, tables, ...}, ...]}

    ``bodies`` 가 1:N 인 이유 — 같은 별표 번호가 시행령·시행규칙으로 갈려
    중복될 수 있다. 사이드카가 없거나 매칭 본문이 없으면 ``bodies`` 는 빈
    리스트, ``body_available`` 은 False — 트리·스키마는 회귀 없이 동작한다.
    """
    if not byeolpyo_refs:
        return []
    if isinstance(byeolpyo_refs, str):
        # 방어: 필드 자체가 bare string 이면 문자 단위 순회를 막는다.
        byeolpyo_refs = [byeolpyo_refs]
    law_bodies = _get_law_bodies(law_name)
    enriched: list = []
    for ref in byeolpyo_refs:
        if isinstance(ref, dict):
            # 이미 enrich 된 항목 — 멱등 통과.
            enriched.append(ref)
            continue
        num = str(ref).strip()
        matches = (law_bodies or {}).get(num, []) if law_bodies else []
        bodies = [_body_summary(m) for m in matches]
        enriched.append(
            {
                "별표": num,
                "body_available": any(b["body_available"] for b in bodies),
                "bodies": bodies,
            }
        )
    return enriched


def warm() -> int:
    """
    별표 본문 사이드카를 전부 미리 로드(예: FastAPI lifespan warmup).

    Returns 로드된(본문이 하나라도 있는) 법령 수. 반복 호출 안전.
    """
    if not _BODIES_DIR.is_dir():
        logger.warning("byeolpyo bodies dir not found: %s", _BODIES_DIR)
        return 0
    for path in sorted(_BODIES_DIR.glob("*.json")):
        _get_law_bodies(path.stem)
    return sum(1 for v in list(_BODIES_BY_LAW.values()) if v)
