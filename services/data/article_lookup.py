"""
Deterministic per-article lookup over the legalize-kr corpus.

kolaw's vector /search returns document-level chunks — it cannot reliably
surface the verbatim text of a *specific* article (e.g. 개인정보보호법 제15조).
This module is a pure file-parse lookup: locate a law's folder under
~/Thairon/legalize-kr/kr/, open the requested markdown file, split on the
제N조 headings with the EXISTING legalize_kr regex, and return the exact
requested article's verbatim text up to the next 제N조 heading.

No embeddings, no vector search — fully deterministic.

Corpus quirk handled here
-------------------------
The legalize-kr markdown headings have the `의N` suffix STRIPPED:
`제7조의2 (보호위원회의 구성 등)` is stored on disk as `제7조 (보호위원회의 구성 등)`.
Korean statutes insert 의-articles (제N조의M) immediately after their base
article in numbering order, so the suffix is recovered POSITIONALLY: the
k-th consecutive `제N조` heading is 제N조 for k==1, 제N조의2 for k==2, etc.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from services.data.legalize_kr import (
    _CORPUS_PATH,
    _parse_frontmatter,
    _split_articles,
)

# Markdown file name per 법령구분 — keys are the `type` query values.
_TYPE_FILES: dict[str, str] = {
    "법률": "법률.md",
    "시행령": "시행령.md",
    "시행규칙": "시행규칙.md",
    "대통령령": "대통령령.md",
    "대법원규칙": "대법원규칙.md",
}

# Parse a requested article reference into (base number, 의-suffix).
#   "제15조"     -> (15, 1)
#   "제14조의2"  -> (14, 2)
#   "15"         -> (15, 1)
#   "제14조의2항" tolerated -> (14, 2)
_ARTICLE_REF_RE = re.compile(r"제?\s*(\d+)\s*조?\s*(?:의\s*(\d+))?")


@dataclass
class ArticleLookupResult:
    found: bool
    law_name: str          # display 법령명 from frontmatter (e.g. "개인정보 보호법")
    law_id: str            # 법령ID, e.g. "011357"
    version: str           # 시행일자 YYYYMMDD
    article: str           # canonical article ref, e.g. "제15조" / "제14조의2"
    title: str             # article title, e.g. "(개인정보의 수집ㆍ이용)"
    text: str              # verbatim article body (항/호 included)
    source_path: str       # absolute markdown file path — provenance
    type: str              # 법률 / 시행령 / ...
    error: str | None = None


def _normalize_name(name: str) -> str:
    """Strip whitespace and NFC-normalize for robust folder matching."""
    return unicodedata.normalize("NFC", name).replace(" ", "").strip()


def _resolve_law_dir(law_name: str) -> Path | None:
    """
    Find a law folder under the corpus.

    legalize-kr folder names carry no spaces (개인정보보호법), but callers and
    law frontmatter titles often include them (개인정보 보호법). macOS stores
    filenames in NFD; queries arrive NFC. Match space-stripped + NFC-folded.
    """
    if not _CORPUS_PATH.exists():
        return None

    target = _normalize_name(law_name)
    if not target:
        return None

    # Fast path: exact (space-stripped) folder hit.
    direct = _CORPUS_PATH / target
    if direct.is_dir():
        return direct

    # Fallback: scan and compare normalized names.
    for entry in _CORPUS_PATH.iterdir():
        if entry.is_dir() and _normalize_name(entry.name) == target:
            return entry
    return None


def _parse_article_ref(article_ref: str) -> tuple[int, int] | None:
    """Return (base_number, eui_suffix) where eui_suffix==1 means no 의."""
    m = _ARTICLE_REF_RE.search(article_ref or "")
    if not m:
        return None
    base = int(m.group(1))
    eui = int(m.group(2)) if m.group(2) else 1
    return base, eui


def _canonical_article(base: int, eui: int) -> str:
    """(15, 1) -> '제15조'; (14, 2) -> '제14조의2'."""
    return f"제{base}조" if eui == 1 else f"제{base}조의{eui}"


def lookup_article(
    law_name: str,
    article_ref: str,
    law_type: str = "법률",
) -> ArticleLookupResult:
    """
    Look up the verbatim text of one article from the legalize-kr corpus.

    Args:
        law_name:    법령명 — folder name or display title (spaces tolerated).
        article_ref: 조문 참조 — "제15조", "제14조의2", or bare "15".
        law_type:    법령 종류 — 법률(default) / 시행령 / 시행규칙 / ...

    Returns:
        ArticleLookupResult. `found` is False with a populated `error` when
        the law or the article cannot be resolved.
    """
    empty = ArticleLookupResult(
        found=False, law_name=law_name, law_id="", version="",
        article=article_ref, title="", text="", source_path="",
        type=law_type,
    )

    parsed = _parse_article_ref(article_ref)
    if parsed is None:
        empty.error = (
            f"조문 참조를 해석할 수 없습니다: '{article_ref}'. "
            "예: '제15조' 또는 '제14조의2'."
        )
        return empty
    base, eui = parsed
    canonical = _canonical_article(base, eui)
    empty.article = canonical

    law_dir = _resolve_law_dir(law_name)
    if law_dir is None:
        empty.error = (
            f"법령을 찾을 수 없습니다: '{law_name}'. "
            "legalize-kr 코퍼스에 해당 법령 폴더가 없습니다."
        )
        return empty

    md_name = _TYPE_FILES.get(law_type)
    md_file: Path | None = None
    if md_name:
        cand = law_dir / md_name
        if cand.exists():
            md_file = cand
    if md_file is None:
        # Requested type missing — fall back to 법률.md, then any .md.
        for fallback in ("법률.md",):
            cand = law_dir / fallback
            if cand.exists():
                md_file = cand
                break
    if md_file is None:
        mds = sorted(law_dir.glob("*.md"))
        if mds:
            md_file = mds[0]
    if md_file is None:
        empty.error = (
            f"법령 '{law_name}' 폴더에 마크다운 파일이 없습니다 "
            f"(요청 종류: {law_type})."
        )
        return empty

    actual_type = md_file.stem  # e.g. "법률"
    text = md_file.read_text(encoding="utf-8")
    meta = _parse_frontmatter(text)
    law_id = meta.get("법령ID", "unknown")
    law_display = meta.get("제목", law_name)
    enforcement = meta.get("시행일자", "").replace("-", "")

    # REUSE legalize_kr's article splitter — do not reinvent the regex.
    articles = _split_articles(text)

    # legalize-kr strips the 의N suffix from headings, so all of 제N조,
    # 제N조의2, 제N조의3 ... appear as bare "제N조". Recover positionally:
    # the eui-th consecutive 제N조 heading is the requested article.
    base_label = f"제{base}조"
    same_base = [a for a in articles if a.number == base_label]

    if not same_base:
        empty.error = (
            f"'{law_display}' {actual_type}에 {base_label} 이(가) 없습니다."
        )
        empty.law_id = law_id
        empty.law_name = law_display
        empty.version = enforcement
        empty.type = actual_type
        empty.source_path = str(md_file)
        return empty

    if eui > len(same_base):
        empty.error = (
            f"'{law_display}' {actual_type}에 {canonical} 이(가) 없습니다 "
            f"({base_label} 계열은 {len(same_base)}개까지 존재)."
        )
        empty.law_id = law_id
        empty.law_name = law_display
        empty.version = enforcement
        empty.type = actual_type
        empty.source_path = str(md_file)
        return empty

    target = same_base[eui - 1]
    return ArticleLookupResult(
        found=True,
        law_name=law_display,
        law_id=law_id,
        version=enforcement,
        article=canonical,
        title=target.title,
        text=target.content,
        source_path=str(md_file),
        type=actual_type,
        error=None,
    )
