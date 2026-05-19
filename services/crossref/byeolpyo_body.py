"""
별표(別表) 본문 추출 모듈 — Phase 2 (A안: 표형 별표만).

``build_index.py`` 의 crossref 색인은 별표를 *카탈로그*(번호·이름·연결조문)
로만 담는다. 별표 *본문*(표 내용)은 빠진다. 이 모듈이 그 빈칸을 채운다.

데이터 경로
-----------
licbyl SEARCH XML(``services/crossref/cache/licbyl/*.xml``)의 각 별표 항목에는
``별표서식PDF파일링크``(예: ``/LSW/flDownload.do?flSeq=163672701``)가 있다.
이 PDF는 born-digital(벡터 텍스트)이라 OCR 없이 ``pdfplumber`` 로 텍스트·표를
바로 뽑을 수 있다. (⚠️ ``pypdf`` 는 한글 띄어쓰기를 뭉갠다 — 쓰지 않는다.)

A안 범위
--------
licbyl ``별표종류`` 가 ``별표``(표형)인 항목만 처리한다. ``별표종류=서식``
(신고서·명령서 같은 양식)은 건너뛴다.

처리 흐름 (법령 1건)
--------------------
1. ``services/crossref/cache/licbyl/<법령>.xml`` 를 읽어 별표 카탈로그를
   파싱(``build_index._parse_licbyl_xml`` 재사용) → ``별표종류=별표`` 만 남김.
2. 각 별표의 ``pdf_url`` PDF를 다운로드. 사이드카 캐시
   ``services/crossref/cache/byeolpyo_pdf/`` 에 ``flSeq`` 키로 저장.
   재실행 시 캐시가 있으면 다운로드 스킵. 연속 HTTP 호출 사이 0.7초 sleep.
3. ``pdfplumber`` 로 텍스트 + 표 추출:
   - 표: ``lines_strict`` 전략으로만 추출(괘선이 실제로 있는 표만 잡힘 —
     ``lines``/``text`` 전략은 헤더 한 줄을 가짜 표로 오탐). 마크다운 표로 정규화.
   - 헤더·문단: ``extract_text()`` 출력을 그대로 보존(띄어쓰기 살아 있음).
4. 이미지형 별표 탐지: ``page.images`` 가 하나라도 있으면 ``is_image: true``
   플래그만 세우고 본문은 비운다(OCR 은 후속 과제, 이번 범위 밖).
5. 산출: 사이드카 JSON ``services/crossref/byeolpyo_bodies/<법령>.json``.

이번 청크는 추출 모듈 + 소수 법령 검증까지다. 전체 5,167건 일괄 실행이나
색인·트리 연결은 다음 청크.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pdfplumber

# build_index 는 같은 패키지(services.crossref) — XML 파서/정규화 헬퍼 재사용.
try:  # 패키지로 import 될 때
    from services.crossref.build_index import _norm_law_name, _parse_licbyl_xml
except ImportError:  # 모듈 단독 실행(python services/crossref/byeolpyo_body.py)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from services.crossref.build_index import _norm_law_name, _parse_licbyl_xml

# ─────────────────────────────────────────────────────────────────────────────
# 경로 / 상수
# ─────────────────────────────────────────────────────────────────────────────

_CROSSREF_DIR = Path(__file__).resolve().parent
_LICBYL_CACHE = _CROSSREF_DIR / "cache" / "licbyl"
_PDF_CACHE = _CROSSREF_DIR / "cache" / "byeolpyo_pdf"
_BODIES_DIR = _CROSSREF_DIR / "byeolpyo_bodies"

# 연속 PDF 다운로드 사이 sleep(초) — law.go.kr 레이트리밋 회피.
_PDF_SLEEP = 0.7
_HTTP_TIMEOUT = 30
# pdfplumber 표 추출: 괘선이 실제로 있는 표만 잡는 전략.
_TABLE_SETTINGS = {
    "vertical_strategy": "lines_strict",
    "horizontal_strategy": "lines_strict",
}
_SCHEMA_VERSION = 1


# ─────────────────────────────────────────────────────────────────────────────
# PDF 다운로드 (사이드카 캐시)
# ─────────────────────────────────────────────────────────────────────────────


def _flseq_from_url(pdf_url: str) -> str | None:
    """다운로드 URL 의 flSeq 쿼리값을 캐시 키로 뽑는다."""
    try:
        qs = urllib.parse.urlparse(pdf_url).query
        seq = urllib.parse.parse_qs(qs).get("flSeq", [""])[0]
    except ValueError:
        return None
    return seq or None


def _download_pdf(pdf_url: str, use_cache: bool = True) -> tuple[Path | None, str | None]:
    """
    별표 PDF 를 받아 ``cache/byeolpyo_pdf/<flSeq>.pdf`` 로 저장.

    Returns (path, error). 캐시가 이미 있으면 다운로드를 건너뛰고 그 경로를
    돌려준다(이때 error 는 None, 호출부가 sleep 을 생략할 수 있도록 함).
    """
    seq = _flseq_from_url(pdf_url)
    if not seq:
        return None, f"flSeq 미검출: {pdf_url}"

    _PDF_CACHE.mkdir(parents=True, exist_ok=True)
    dest = _PDF_CACHE / f"{seq}.pdf"
    if use_cache and dest.exists() and dest.stat().st_size > 0:
        return dest, None

    req = urllib.request.Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        return None, f"PDF 다운로드 실패({seq}): {exc}"

    # 실제 네트워크 호출을 했으므로 — PDF 검증 성공 여부와 무관하게 — sleep.
    # 캐시 히트(위에서 early-return)는 이 지점에 오지 않으므로 즉시 진행된다.
    time.sleep(_PDF_SLEEP)

    if "pdf" not in ctype:
        # law.go.kr 가 PDF 대신 에러 HTML 페이지를 돌려준 경우.
        return None, f"PDF 아님(Content-Type={ctype}, flSeq={seq})"
    if not data.startswith(b"%PDF"):
        return None, f"PDF 시그니처 불일치(flSeq={seq})"

    dest.write_bytes(data)
    return dest, None


# ─────────────────────────────────────────────────────────────────────────────
# pdfplumber 추출
# ─────────────────────────────────────────────────────────────────────────────


def _table_to_markdown(table: list[list[str | None]]) -> str:
    """pdfplumber 표(행 리스트)를 마크다운 표 문자열로 정규화."""
    rows: list[list[str]] = []
    for raw_row in table:
        cells = [
            (cell or "").replace("\n", " ").replace("|", "\\|").strip()
            for cell in raw_row
        ]
        rows.append(cells)
    if not rows:
        return ""

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    header = rows[0]
    out = ["| " + " | ".join(header) + " |"]
    out.append("| " + " | ".join(["---"] * width) + " |")
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _extract_pdf(pdf_path: Path) -> dict:
    """
    PDF 1건에서 별표 본문을 뽑는다.

    Returns dict:
        {is_image, page_count, image_count, text, tables[]}
      - is_image True 면 본문(text/tables)은 비운다.
      - text   : extract_text() 페이지별 결과를 줄바꿈 2개로 이어붙인 것.
      - tables : 각 페이지의 lines_strict 추출 표를 마크다운으로 정규화한 리스트.
                 의미 없는 표(1행 또는 단일 열)는 버린다.
    """
    page_texts: list[str] = []
    tables_md: list[dict] = []
    image_count = 0

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for pidx, page in enumerate(pdf.pages):
            image_count += len(page.images)
            txt = page.extract_text() or ""
            if txt.strip():
                page_texts.append(txt)
            for table in page.extract_tables(_TABLE_SETTINGS):
                # 다열·다행이 있는 표만 의미 있는 데이터로 본다.
                if len(table) < 2 or not any(len(r) > 1 for r in table):
                    continue
                md = _table_to_markdown(table)
                if md:
                    tables_md.append({"page": pidx, "markdown": md})

    if image_count > 0:
        # 이미지형 별표 — 본문은 비우고 플래그만. OCR 은 후속 과제.
        return {
            "is_image": True,
            "page_count": page_count,
            "image_count": image_count,
            "text": "",
            "tables": [],
        }

    return {
        "is_image": False,
        "page_count": page_count,
        "image_count": 0,
        "text": "\n\n".join(page_texts),
        "tables": tables_md,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 법령 1건 처리
# ─────────────────────────────────────────────────────────────────────────────


def _is_moved_placeholder(name: str) -> bool:
    """'[별표 1] 로 이동' 같은 redirect 스텁 — 본문 없음, 건너뛴다."""
    return "로 이동" in name or "으로 이동" in name


def extract_law_byeolpyo(
    law_name: str,
    use_cache: bool = True,
) -> tuple[dict | None, str | None]:
    """
    한 법령의 캐시된 licbyl XML 에서 표형(별표종류=별표) 별표 본문을 모두 추출.

    Returns (sidecar_dict, error). sidecar_dict 스키마는 ``_BODIES_DIR`` 에
    저장되는 JSON 과 동일하다(아래 ``build_law_sidecar`` 참고).
    """
    folded = _norm_law_name(law_name)
    xml_path = _LICBYL_CACHE / f"{folded}.xml"
    if not xml_path.exists():
        return None, f"licbyl 캐시 없음: {xml_path.name}"

    try:
        raw = xml_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"licbyl XML 읽기 실패: {exc}"

    entries, err = _parse_licbyl_xml(raw)
    if err:
        return None, err

    # A안: 표형 별표만.
    table_entries = [e for e in entries if e.get("별표종류") == "별표"]

    items: list[dict] = []
    for entry in table_entries:
        name = entry.get("별표명", "")
        item: dict = {
            "별표": entry.get("별표"),
            "별표번호": entry.get("별표번호"),
            "별표명": name,
            "관련법령명": entry.get("관련법령명"),
            "attached_article": entry.get("attached_article"),
            "별표일련번호": entry.get("별표일련번호"),
            "pdf_url": entry.get("pdf_url", ""),
            "hwp_url": entry.get("hwp_url", ""),
            "is_image": False,
            "page_count": 0,
            "text": "",
            "tables": [],
            "error": None,
        }

        if _is_moved_placeholder(name):
            item["error"] = "이동(redirect) 항목 — 본문 없음, 스킵"
            items.append(item)
            continue

        pdf_url = entry.get("pdf_url", "")
        if not pdf_url:
            item["error"] = "별표서식PDF파일링크 없음"
            items.append(item)
            continue

        pdf_path, derr = _download_pdf(pdf_url, use_cache=use_cache)
        if derr:
            item["error"] = derr
            items.append(item)
            continue

        try:
            extracted = _extract_pdf(pdf_path)
        except Exception as exc:  # noqa: BLE001 — pdfplumber 내부 예외 다양
            item["error"] = f"pdfplumber 추출 실패: {exc}"
            items.append(item)
            continue

        item.update({
            "is_image": extracted["is_image"],
            "page_count": extracted["page_count"],
            "image_count": extracted["image_count"],
            "text": extracted["text"],
            "tables": extracted["tables"],
        })
        items.append(item)

    sidecar = {
        "schema_version": _SCHEMA_VERSION,
        "법령명": law_name,
        "법령명_folded": folded,
        "scope": "A안: 표형 별표만(별표종류=별표)",
        "byeolpyo_count": len(items),
        "byeolpyo": items,
    }
    return sidecar, None


def build_law_sidecar(
    law_name: str,
    use_cache: bool = True,
) -> tuple[Path | None, str | None]:
    """한 법령을 추출해 사이드카 JSON 으로 저장. Returns (path, error)."""
    sidecar, err = extract_law_byeolpyo(law_name, use_cache=use_cache)
    if err:
        return None, err

    _BODIES_DIR.mkdir(parents=True, exist_ok=True)
    out = _BODIES_DIR / f"{sidecar['법령명_folded']}.json"
    out.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out, None


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _summarize(sidecar: dict) -> str:
    items = sidecar["byeolpyo"]
    img = sum(1 for i in items if i.get("is_image"))
    errs = sum(1 for i in items if i.get("error"))
    ok = len(items) - errs
    tbls = sum(len(i.get("tables", [])) for i in items)
    return (
        f"{sidecar['법령명']}: 표형 별표 {len(items)}건 "
        f"(추출성공 {ok}, 이미지형 {img}, 오류/스킵 {errs}, 추출 표 {tbls}개)"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="별표(別表) 본문 추출 — 표형 별표만(A안). "
        "licbyl 캐시 XML → PDF 다운로드 → pdfplumber → 사이드카 JSON.",
    )
    ap.add_argument(
        "laws",
        nargs="+",
        help="법령명(공백 무시·NFC 정규화됨). licbyl 캐시 XML 이 있어야 함.",
    )
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="캐시된 PDF 를 무시하고 다시 다운로드.",
    )
    args = ap.parse_args(argv)

    rc = 0
    for law in args.laws:
        out, err = build_law_sidecar(law, use_cache=not args.no_cache)
        if err:
            print(f"[실패] {law}: {err}", file=sys.stderr)
            rc = 1
            continue
        sidecar = json.loads(out.read_text(encoding="utf-8"))
        print(f"[완료] {_summarize(sidecar)}")
        print(f"        → {out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
