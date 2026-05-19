"""
법령 간 인용 그래프 색인 — chunk 1 빌더.

법률도서관(legalize-kr 코퍼스)의 각 법령 본문에서 「법령명」 제N조 형태의
다른 법령 인용을 추출해, 법령·조문 사이의 가로(horizontal) 인용 간선을 만든다.
build_index.py 의 위임(세로) 색인과 짝을 이룬다.

ONE 법령 폴더에 대해
--------------------
1. 폴더 안 모든 법령 본문 .md (법률·시행령·시행규칙·대통령령·대법원규칙·각
   부령 등) 의 조문을 _split_articles 로 분리. build_index.py 와 동일하게
   부칙 제거, 의M 위치 복원, doc_id 동일 스킴.
2. 조문 본문에서 「법령명」[ 제N조[의M][제K항][제K호]] 인용 패턴 추출.
3. 도착 법령명을 코퍼스 폴더로 해소 — 약칭·띄어쓰기·"시행령/시행규칙" 접미사 정규화.
   - 해소되면 target_resolved=true, 도착 폴더·파일 종류 기록.
   - 해소 안 되면(도서관 미보유) target_resolved=false, 법령명·조문만 보존.
     인용 사실은 버리지 않는다 (의장 결정 2026-05-19).
   - 같은 폴더 자기 인용(본법↔시행령 등)은 세로 관계 → 인용 그래프에서 제외.
4. 인용 직후 같은 문장에 "준용" 이 있으면 strength=strong, 아니면 weak.
5. 동일 (출발 조문, 도착 법, 도착 파일, 도착 조문) 간선은 count 로 합산
   (한 번이라도 strong 이면 strong).
6. 법령당 사이드카 JSON ``services/crossref/citations/<folder>.json`` 출력.

chunk 1 = 빌더 + 5개 법령 수기 검증. SQLite 통합(양방향 조회)·API 노출은
chunk 2/3 에서. 인용 간선은 (폴더, 파일종류, 조문)의 *논리 참조* 만 저장한다 —
도착 조문 doc_id 는 굳이 굽지 않고 조회 시 /article 경로로 해소한다.

알려진 한계 (chunk 1)
---------------------
* "「민법」 제750조 및 제751조" 처럼 한 낫표 뒤 여러 조문이 이어지면 첫 조문만
  잡는다. 5개 법령 검증에서 빈도를 보고 chunk 2 확장 여부 결정.
* 「」 는 한국 법령문에서 법령 제목 표기 관례지만 조약·고시 제목도 묶일 수 있다 —
  해소 안 되는 이름은 unresolved_names 로 남겨 검증 때 눈으로 확인한다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# Make the kolaw repo root importable when run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.data.legalize_kr import (  # noqa: E402
    _CORPUS_PATH,
    _parse_frontmatter,
    _split_articles,
)
from services.crossref.build_index import (  # noqa: E402
    _doc_id_for,
    _norm_law_name,
    _restore_eui,
    _strip_buchik,
)

# ─────────────────────────────────────────────────────────────────────────────
# Regexes
# ─────────────────────────────────────────────────────────────────────────────

# 「법령명」[ 제N조[의M][제K항][제K호]] — 가로 인용 패턴.
# 낫표 안은 줄바꿈 없이 2~60자. 조문 참조는 같은 줄에 이어질 때만 함께 잡는다
# ([ \t]* — 줄바꿈을 건너 다음 줄 조문을 잘못 붙이지 않게).
_CITE_RE = re.compile(
    r"「([^」\n]{2,120})」"
    r"(?:[ \t]*(제\d+조(?:의\d+)?(?:제\d+항)?(?:제\d+호)?))?"
)

# 인용 조문 토큰에서 그래프 링크 단위(제N조[의M])만 — 항·호는 버린다.
_ART_BASE_RE = re.compile(r"(제\d+조(?:의\d+)?)")

# 준용 문맥 판정 시 문장 경계 — 마침표·줄바꿈에서 끊는다.
# (다음 인용 「 까지 경계로 넣으면 '「A」 및 「B」를 준용한다' 같은 흔한 병렬
#  준용에서 앞 인용 A 를 false weak 로 만든다 — 앞 인용으로 새는 드문 오판보다
#  손실이 커서 「 는 경계로 쓰지 않는다. 병렬·조건절 정밀 판정은 chunk 2.)
_SENT_CUT_RE = re.compile(r"[.\n]")

# 부정 준용 — '준용하지 아니', '준용되지 아니', '준용하지(는/도) 않' 등.
# 부정 준용은 의미가 반대(적용 안 함)이므로 strong 으로 보지 않는다.
_NEG_JUNYONG_RE = re.compile(r"준용(?:하|되)지(?:는|도)?\s*(?:아니|않)")

# 약칭 → 정식 법령명 (자주 쓰는 것만; lawxref.sh ALIASES 포팅).
_ALIASES = {
    "수소법": "수소경제육성및수소안전관리에관한법률",
    "고압가스법": "고압가스안전관리법",
    "도시가스법": "도시가스사업법",
    "개인정보법": "개인정보보호법",
    "정보통신망법": "정보통신망이용촉진및정보보호등에관한법률",
    "전자상거래법": "전자상거래등에서의소비자보호에관한법률",
    "자본시장법": "자본시장과금융투자업에관한법률",
    "공정거래법": "독점규제및공정거래에관한법률",
    "독점규제법": "독점규제및공정거래에관한법률",
}

# 도착 법령명에서 떼어낼 하위 법령 접미사 → 파일 종류. 긴 것 먼저.
_SUFFIX_FILE = (
    ("시행규칙", "시행규칙"),
    ("시행령", "시행령"),
)


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _SrcArticle:
    """인용 출발 조문 — doc_id 는 ingest 와 동일 스킴."""

    file_type: str
    doc_id: str
    canonical: str   # 의M 복원된 표기 — 제28조의8
    title: str
    content: str


# ─────────────────────────────────────────────────────────────────────────────
# 해소 (법령명 → 코퍼스 폴더)
# ─────────────────────────────────────────────────────────────────────────────


def build_folder_index() -> dict[str, tuple[str, tuple[str, ...]]]:
    """
    norm(법령명) -> (코퍼스 폴더명 NFC, 폴더 안 .md stem 튜플).

    전 폴더 1회 스캔해 만든 해소 테이블. 폴더명은 디스크에 NFD(분해형)로
    저장돼 있어 NFC 정규화한다 — 안 그러면 사이드카의 target_law_folder 가
    NFD 가 돼 chunk 2 SQLite 조인이 조용히 어긋난다. .md stem 집합은
    _resolve_target 이 도착 파일 종류를 폴더의 실제 내용에서 정하는 데 쓴다.
    """
    idx: dict[str, tuple[str, tuple[str, ...]]] = {}
    if not _CORPUS_PATH.is_dir():
        return idx
    for entry in _CORPUS_PATH.iterdir():
        if entry.is_dir():
            stems = tuple(sorted(p.stem for p in entry.glob("*.md")))
            idx[_norm_law_name(entry.name)] = (
                unicodedata.normalize("NFC", entry.name),
                stems,
            )
    return idx


def _alias_norm(name: str) -> str:
    """법령명 → 정규화 키 (NFC·공백제거·약칭 치환)."""
    norm = _norm_law_name(name)
    return _norm_law_name(_ALIASES.get(norm, norm))


def _pick_file_type(stems: tuple[str, ...], prefer: str | None) -> str:
    """
    도착 폴더의 .md stem 집합에서 도착 파일 종류를 고른다.

    prefer(인용에 '시행령/시행규칙' 접미사가 있었으면 그 종류)가 폴더에 실제로
    있으면 그것을, 없으면 폴더의 주(主) 법령문서를 고른다 — 모범공무원규정처럼
    법률.md 없이 대통령령.md 만 있는 폴더는 '대통령령'. 추측("법률" 고정)하면
    chunk 3 의 본문 조회가 실제 파일과 어긋난다.
    """
    if prefer and prefer in stems:
        return prefer
    for cand in ("법률", "시행령", "시행규칙", "대통령령", "대법원규칙"):
        if cand in stems:
            return cand
    return stems[0] if stems else "법률"


def _resolve_target(
    raw_name: str,
    folder_index: dict[str, tuple[str, tuple[str, ...]]],
) -> tuple[str | None, str]:
    """
    도착 법령명(낫표 안 원문) → (코퍼스 폴더명 또는 None, 파일 종류).

    1) 낫표 안 이름 *전체* 를 그대로 폴더로 찾는다 — 코퍼스에는 '○○법시행령'
       처럼 시행령이 독립 폴더인 경우가 있어, 접미사를 먼저 떼면 base 폴더로
       잘못 해소(false positive)된다.
    2) 전체 이름이 폴더가 아니면 '시행령/시행규칙' 접미사를 떼고 base 법령
       폴더를 찾는다 (시행령이 base 폴더 안 .md 인 경우).
    파일 종류는 추측이 아니라 도착 폴더의 실제 .md 에서 고른다(_pick_file_type).
    못 찾으면 (None, "법률"). 약칭·NFC·공백은 _alias_norm 으로 정규화.
    """
    name = raw_name.strip()
    suffix_intent: str | None = None
    for suffix, ft in _SUFFIX_FILE:
        if name.endswith(suffix):
            suffix_intent = ft
            break
    # 1) 전체 이름이 곧 폴더인가?
    entry = folder_index.get(_alias_norm(name))
    if entry is not None:
        folder, stems = entry
        return folder, _pick_file_type(stems, suffix_intent)
    # 2) 접미사를 떼고 base 법령 폴더 시도.
    if suffix_intent is not None and len(name) > len(suffix_intent):
        base = name[: -len(suffix_intent)].strip()
        entry = folder_index.get(_alias_norm(base))
        if entry is not None:
            folder, stems = entry
            return folder, _pick_file_type(stems, suffix_intent)
    return None, "법률"


def _target_article(token: str | None) -> str | None:
    """'제28조의8제2항제3호' → '제28조의8'. 항·호는 그래프 링크 단위에서 버린다."""
    if not token:
        return None
    m = _ART_BASE_RE.match(token)
    return m.group(1) if m else None


def _strength_at(content: str, end: int) -> str:
    """
    인용 직후 같은 문장(마침표·줄바꿈 이전)에 '준용' 이 있으면 strong.

    부정 준용('준용하지 아니한다' 등)은 의미가 반대이므로 weak 로 본다.
    한 문장에 인용이 여럿일 때 뒤 인용의 준용이 앞 인용으로 새는 오판,
    조건절 오판 등 잔여 휴리스틱 한계는 chunk 2 인용추출 보강 패스로 미룬다.
    """
    window = content[end : end + 120]
    cut = _SENT_CUT_RE.search(window)
    clause = window[: cut.start()] if cut else window
    if "준용" not in clause:
        return "weak"
    if _NEG_JUNYONG_RE.search(clause):
        return "weak"
    return "strong"


# ─────────────────────────────────────────────────────────────────────────────
# 조문 순회
# ─────────────────────────────────────────────────────────────────────────────


def _iter_source_articles(law_dir: Path):
    """
    폴더 안 모든 법령 본문 .md 의 조문을 doc_id 와 함께 yield.

    법령 폴더는 법률.md/시행령.md/시행규칙.md 외에 대통령령.md·대법원규칙.md·
    각 부령 등 다양한 본문 stem 을 가진다 — 셋만 보면 코퍼스의 약 40%를
    인용 출발에서 놓친다. *.md 를 전부 스캔하고 파일 stem 을 파일 종류로 쓴다.
    """
    folder_slug = law_dir.name[:20].replace(" ", "_")
    for md in sorted(law_dir.glob("*.md")):
        file_type = md.stem
        text = md.read_text(encoding="utf-8")
        meta = _parse_frontmatter(text)
        # doc_id 스킴은 파일별 frontmatter 의 법령ID 를 쓴다 (build_index 와 동일).
        law_id = meta.get("법령ID", "") or f"folder_{hash(law_dir.name) & 0xFFFFFF:06x}"
        body = _strip_buchik(text)
        raw_articles = _split_articles(body)
        canon = _restore_eui([a.number for a in raw_articles])
        used: set[str] = set()
        for art, canonical in zip(raw_articles, canon):
            doc_id = _doc_id_for(law_id, folder_slug, file_type, art.number, used)
            yield _SrcArticle(file_type, doc_id, canonical, art.title, art.content)


# ─────────────────────────────────────────────────────────────────────────────
# 법령 1건 인용 색인
# ─────────────────────────────────────────────────────────────────────────────


def _find_law_dir(law_folder: str) -> Path:
    """코퍼스에서 법령 폴더를 찾는다 (NFC 정규화·공백 허용)."""
    target = unicodedata.normalize("NFC", law_folder).replace(" ", "").strip()
    direct = _CORPUS_PATH / target
    if direct.is_dir():
        return direct
    for entry in _CORPUS_PATH.iterdir():
        if entry.is_dir() and _norm_law_name(entry.name) == target:
            return entry
    raise FileNotFoundError(
        f"법령 폴더를 찾을 수 없습니다: '{law_folder}' (corpus: {_CORPUS_PATH})"
    )


def build_law_citations(law_folder: str, folder_index: dict[str, str]) -> dict:
    """
    한 법령 폴더의 가로 인용 색인을 만든다.

    Returns: JSON 직렬화 가능한 dict — 이 법령의 인용 사이드카.
    Raises: FileNotFoundError — 폴더 없음 또는 본문 .md 가 하나도 없음.
    """
    law_dir = _find_law_dir(law_folder)

    md_files = sorted(law_dir.glob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"본문 .md 없음: {law_dir}")
    # law_name/law_id 메타는 주 법령문서에서 — 법률>시행령>시행규칙>대통령령>
    # 대법원규칙 순으로 고르고, 그 외 stem 뿐이면 첫 .md (설명용 .md 혼입 방어).
    by_stem = {m.stem: m for m in md_files}
    primary_md = next(
        (by_stem[s] for s in
         ("법률", "시행령", "시행규칙", "대통령령", "대법원규칙")
         if s in by_stem),
        md_files[0],
    )
    meta = _parse_frontmatter(primary_md.read_text(encoding="utf-8"))

    law_name = meta.get("제목", law_dir.name)
    law_id = meta.get("법령ID", "")
    src_folder_norm = _norm_law_name(law_dir.name)

    # 동일 간선 합산: key -> edge dict.
    agg: dict[tuple, dict] = {}
    articles_scanned = 0
    self_cites = 0

    for src in _iter_source_articles(law_dir):
        articles_scanned += 1
        for m in _CITE_RE.finditer(src.content):
            raw_name = m.group(1).strip()
            if not raw_name:
                continue
            tgt_folder, tgt_ftype = _resolve_target(raw_name, folder_index)
            # 같은 법령 폴더 자기 인용 → 세로 관계, 가로 그래프에서 제외.
            if tgt_folder is not None and _norm_law_name(tgt_folder) == src_folder_norm:
                self_cites += 1
                continue
            tgt_article = _target_article(m.group(2))
            strength = _strength_at(src.content, m.end())
            resolved = tgt_folder is not None
            key = (
                src.doc_id,
                tgt_folder if resolved else f"raw:{_norm_law_name(raw_name)}",
                tgt_ftype,
                tgt_article or "",
            )
            edge = agg.get(key)
            if edge is None:
                agg[key] = {
                    "src_doc_id": src.doc_id,
                    "src_article": src.canonical,
                    "src_file_type": src.file_type,
                    "src_title": src.title,
                    "target_law_raw": raw_name,
                    "target_law_folder": tgt_folder,
                    "target_file_type": tgt_ftype,
                    "target_resolved": resolved,
                    "target_article": tgt_article,
                    "strength": strength,
                    "count": 1,
                }
            else:
                edge["count"] += 1
                if strength == "strong":
                    edge["strength"] = "strong"

    outbound = sorted(
        agg.values(),
        key=lambda e: (
            e["src_doc_id"],
            e["target_law_folder"] or e["target_law_raw"],
            e["target_file_type"],
            e["target_article"] or "",
        ),
    )
    unresolved_names = sorted(
        {e["target_law_raw"] for e in outbound if not e["target_resolved"]}
    )

    return {
        "schema": "kolaw-citation/v1",
        "law_folder": unicodedata.normalize("NFC", law_dir.name),
        "law_name": law_name,
        "law_id": law_id,
        "counts": {
            "articles_scanned": articles_scanned,
            "outbound_edges": len(outbound),
            "resolved_edges": sum(1 for e in outbound if e["target_resolved"]),
            "unresolved_edges": sum(1 for e in outbound if not e["target_resolved"]),
            "strong_edges": sum(1 for e in outbound if e["strength"] == "strong"),
            "self_citations_skipped": self_cites,
            "distinct_target_laws": len(
                {(e["target_law_folder"] or e["target_law_raw"]) for e in outbound}
            ),
        },
        "outbound": outbound,
        "unresolved_names": unresolved_names,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 출력 / CLI
# ─────────────────────────────────────────────────────────────────────────────


def _citations_dir() -> Path:
    """인용 사이드카 JSON 기본 출력 디렉터리."""
    return Path(__file__).resolve().parent / "citations"


def _print_law_summary(index: dict, out_path: Path) -> None:
    c = index["counts"]
    print(f"[citation] {index['law_name']} ({index['law_folder']})")
    print(f"  조문 {c['articles_scanned']}개 스캔")
    print(f"  인용 간선 {c['outbound_edges']}개 "
          f"(해소 {c['resolved_edges']} / 미보유 {c['unresolved_edges']} "
          f"/ 준용 strong {c['strong_edges']})")
    print(f"  인용 법령 {c['distinct_target_laws']}개 "
          f"/ 자기 인용 제외 {c['self_citations_skipped']}건")
    if index["unresolved_names"]:
        head = index["unresolved_names"][:12]
        more = len(index["unresolved_names"]) - len(head)
        print(f"  미보유 법령명({len(index['unresolved_names'])}): "
              f"{', '.join(head)}{f' 외 {more}' if more > 0 else ''}")
    print(f"  -> {out_path}")


def run_all(force: bool = False, limit: int | None = None,
            progress_every: int = 50) -> int:
    """코퍼스 전체 법령 폴더의 인용 색인을 일괄 생성 (resume-safe)."""
    if not _CORPUS_PATH.is_dir():
        print(f"[citation] 코퍼스 경로 없음: {_CORPUS_PATH}", file=sys.stderr)
        return 1
    folder_index = build_folder_index()
    folders = sorted(
        (e for e in _CORPUS_PATH.iterdir() if e.is_dir()), key=lambda p: p.name
    )
    if limit is not None:
        folders = folders[:limit]
    total = len(folders)
    out_dir = _citations_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[citation] --all 배치 시작 — 대상 {total}개 폴더 (force={force})",
          file=sys.stderr)

    built = resumed = no_body = 0
    failed: list[tuple[str, str]] = []
    total_edges = 0
    for i, folder in enumerate(folders, start=1):
        if i % progress_every == 0 or i == total:
            print(f"[진행] {i}/{total} ... (생성 {built} / 스킵 {resumed} "
                  f"/ 본문없음 {no_body} / 실패 {len(failed)})", file=sys.stderr)
        out_path = out_dir / f"{folder.name}.json"
        if out_path.exists() and not force:
            resumed += 1
            continue
        try:
            index = build_law_citations(folder.name, folder_index)
            out_path.write_text(
                json.dumps(index, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except FileNotFoundError as exc:
            if "본문 .md 없음" in str(exc):
                no_body += 1
            else:
                failed.append((folder.name, f"{type(exc).__name__}: {exc}"))
            continue
        except Exception as exc:  # noqa: BLE001 — per-law fault isolation
            failed.append((folder.name, f"{type(exc).__name__}: {exc}"))
            continue
        built += 1
        total_edges += index["counts"]["outbound_edges"]

    print(file=sys.stderr)
    print("[citation] --all 배치 완료", file=sys.stderr)
    print(f"  대상 폴더    {total}", file=sys.stderr)
    print(f"  신규 생성    {built}", file=sys.stderr)
    print(f"  재실행 스킵  {resumed}", file=sys.stderr)
    print(f"  본문 없음    {no_body}", file=sys.stderr)
    print(f"  실패        {len(failed)}", file=sys.stderr)
    print(f"  총 인용 간선 {total_edges} (이번 실행 신규 생성분)", file=sys.stderr)
    if failed:
        print("  실패 목록:", file=sys.stderr)
        for name, err in failed:
            print(f"    - {name}: {err}", file=sys.stderr)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="법령 간 인용 그래프 색인 빌더 (chunk 1)."
    )
    parser.add_argument("law_folder", nargs="?", default=None,
                        help="법령 폴더명 (legalize-kr corpus). --all 시 생략.")
    parser.add_argument("-o", "--out", type=Path, default=None,
                        help="출력 JSON 경로 (단일 법령 모드에서만 유효).")
    parser.add_argument("--all", action="store_true",
                        help="코퍼스 전체 법령 폴더를 일괄 색인.")
    parser.add_argument("--force", action="store_true",
                        help="--all 배치에서 이미 있는 JSON 도 다시 생성.")
    parser.add_argument("--limit", type=int, default=None,
                        help="--all 배치에서 처리할 폴더 수 상한 (검증용 슬라이스).")
    parser.add_argument("--progress-every", type=int, default=50,
                        help="--all 진행 로그 간격 (기본 50).")
    args = parser.parse_args()

    if args.all:
        return run_all(force=args.force, limit=args.limit,
                       progress_every=max(1, args.progress_every))

    if not args.law_folder:
        parser.error("law_folder 를 지정하거나 --all 을 사용하세요.")

    folder_index = build_folder_index()
    index = build_law_citations(args.law_folder, folder_index)
    out_path = args.out or (_citations_dir() / f"{index['law_folder']}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_law_summary(index, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
