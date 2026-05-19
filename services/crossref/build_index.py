"""
Delegation cross-reference index builder — Phase 1.

Builds a sidecar JSON index linking a primary law's articles (법률.md) to the
articles in its 시행령.md / 시행규칙.md that the primary law delegates to, plus
별표 references. The goal is to recover the 위임(delegation) relationship that
the flat per-document ChromaDB index drops.

What it does for ONE law folder
-------------------------------
1. Split each markdown file (법률 / 시행령 / 시행규칙) into articles, REUSING
   ``services.data.legalize_kr._split_articles`` — the same regex the ingest
   pipeline uses.
2. Restore the ``의M`` suffix positionally. legalize-kr strips ``의M`` from
   headings (제7조의2 -> 제7조), so the k-th consecutive "제N조" heading is
   제N조 for k==1, 제N조의2 for k==2, ... This mirrors the logic in
   ``services.data.article_lookup``.
3. Exclude the 부칙(附則) section — everything from the first ``부칙`` heading
   onward is dropped as noise.
4. From 법률.md extract delegation signals per article:
   - 대통령령 delegation  ("대통령령으로 정한다" etc.)
   - 총리령/부령 delegation ("총리령으로 정한다" / "OO부령으로 정한다")
   - 별표 references       ("별표 1", "별표 2와 같다", ...)
5. From 시행령.md / 시행규칙.md extract back-references to the primary law
   ("법 제N조", "법 제N조의M제K항") and to the decree ("영 제N조" — for
   시행규칙 -> 시행령 links).
6. Match: a primary-law article that delegates to 대통령령 is linked to every
   시행령 article whose back-reference points at that primary-law article.
   Similarly 시행규칙 articles back-referencing 법 제N조 are linked, and the
   시행규칙 -> 시행령 ("영 제N조") links chain the third tier.
7. Emit a sidecar JSON index keyed by ChromaDB doc_id (the exact id scheme
   from ``services.fast_search.ingest_legalize_kr._law_docs``).

별표(別表) — best-effort
------------------------
별표 bodies are NOT in the legalize-kr corpus. With ``--byeolpyo`` the builder
queries law.go.kr's DRF ``licbyl`` SEARCH endpoint (``target=licbyl&search=2``),
which reliably returns the 별표 *catalogue* — for each 별표: its number, name,
and the article it is attached to (parsed from "(제N조 관련)" in 별표명). That
catalogue is matched onto the decree articles. The 별표 *body text* itself is
served only as an HWP/image attachment behind a JS web page (``lsBylInfoP.do``),
not as DRF API data — so bodies are left unresolved in Phase 1 (see report).

별표 numbering
~~~~~~~~~~~~~~
The corpus body cites 별표 as ``별표 2`` / ``별표 7의2`` (ordinal + optional
의M). law.go.kr's licbyl ``별표번호`` is a fixed 6-digit code: the first 4
digits are the ordinal number, the last 2 are the 의M suffix (``00`` = none).
``000200`` -> 별표 2, ``000702`` -> 별표 7의2. Both are normalized to the same
canonical token (e.g. ``"7의2"``) so the corpus reference and the licbyl
catalogue entry line up. ``search=2`` returns 별표 for *every* law whose name
matches and across decree levels, so entries are filtered by 관련법령명.

This is Phase 1: builder + single-law verification. No API wiring, no UI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
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

# ─────────────────────────────────────────────────────────────────────────────
# Regexes
# ─────────────────────────────────────────────────────────────────────────────

# 부칙 section heading — everything from here on is dropped.
# Verified across the 5 sample laws x {법률,시행령,시행규칙}: the 부칙 section
# always opens with a level-2 heading "## 부칙". The body text of ordinary
# articles also mentions "부칙" (e.g. "...일부개정법률 부칙 제15조..."), but
# those are never at line-start behind a "#" run, so anchoring with
# ^#{1,6}\s* keeps them out. .search() takes the FIRST such heading, which is
# the real 부칙 boundary.
_BUCHIK_RE = re.compile(r"^#{1,6}\s*부칙", re.MULTILINE)

# Delegation signals inside the primary law (per-article body text).
# 대통령령 — "대통령령으로 정한다 / 정하는 / 정한 ..."
_DELEG_PRESIDENTIAL_RE = re.compile(r"대통령령으로\s*정")
# 총리령 / OO부령 — "총리령으로 정한다", "행정안전부령으로 정한다", ...
_DELEG_MINISTERIAL_RE = re.compile(r"(?:총리령|[가-힣]*부령)으로\s*정")
# 별표 N(의M) — "별표 1", "별표 12와 같다", "별표 7의2". group(1)=ordinal,
# group(2)=의M suffix (optional). The 의M MUST be captured: the corpus cites
# 별표 7의2 distinctly from 별표 7.
_BYEOLPYO_RE = re.compile(r"별표\s*(\d+)(?:의(\d+))?")
# 별지 서식 — "별지 제1호서식", "별지 제2호의2서식"
_BYEOLJI_RE = re.compile(r"별지\s*제?\s*(\d+)(?:의(\d+))?\s*호?\s*서식")

# Back-reference inside 시행령 / 시행규칙: "법 제15조", "법 제28조의8제2항".
# group(1)=base number, group(2)=의M (optional), group(3)=제K항 (optional)
_BACKREF_LAW_RE = re.compile(
    r"법\s*제(\d+)조(?:의(\d+))?(?:제(\d+)항)?"
)
# Back-reference to the 시행령 itself, used by 시행규칙: "영 제5조".
_BACKREF_DECREE_RE = re.compile(
    r"영\s*제(\d+)조(?:의(\d+))?(?:제(\d+)항)?"
)

# "(제32조제4항 관련)" inside a 별표명 — the article the 별표 is attached to.
_BYEOLPYO_ATTACH_RE = re.compile(r"제(\d+)조(?:의(\d+))?")

# law.go.kr DRF API — licbyl(별표서식) catalogue search.
_DRF_OC = "Hydrogen"
_DRF_LICBYL_SEARCH = "https://www.law.go.kr/DRF/lawSearch.do"
# Polite delay (seconds) between consecutive licbyl HTTP calls — keeps the
# full 2,302-law expansion under law.go.kr's rate limit.
_LICBYL_SLEEP = 0.7


# ─────────────────────────────────────────────────────────────────────────────
# 별표 numbering helpers
# ─────────────────────────────────────────────────────────────────────────────


def _byeolpyo_token(base: int, eui: int | None) -> str:
    """Canonical 별표 token: 2 -> '2', (7, 2) -> '7의2'."""
    return str(base) if not eui else f"{base}의{eui}"


def _decode_licbyl_no(code: str) -> str | None:
    """
    Decode a law.go.kr licbyl ``별표번호`` 6-digit code to a canonical token.

    The code is ``NNNNMM``: the first 4 digits are the ordinal 별표/서식
    number, the last 2 are the 의M suffix (``00`` = no suffix).
        '000200' -> '2'
        '000702' -> '7의2'
        '001204' -> '12의4'
    Returns None if the code is not a parseable 6-digit string.
    """
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return None
    base = int(code[:4])
    eui = int(code[4:])
    if base == 0:
        return None
    return _byeolpyo_token(base, eui or None)


def _norm_law_name(name: str) -> str:
    """NFC-fold and strip spaces — for comparing 법령명 across sources."""
    return unicodedata.normalize("NFC", name or "").replace(" ", "").strip()


def _byeolpyo_tokens(text: str) -> list[str]:
    """Extract sorted, de-duplicated 별표 tokens from a block of text."""
    found = {
        _byeolpyo_token(int(b), int(e) if e else None)
        for b, e in _BYEOLPYO_RE.findall(text)
    }
    return _sorted_byeolpyo(found)


def _byeolji_tokens(text: str) -> list[str]:
    """Extract sorted, de-duplicated 별지서식 tokens from a block of text."""
    found = {
        _byeolpyo_token(int(b), int(e) if e else None)
        for b, e in _BYEOLJI_RE.findall(text)
    }
    return _sorted_byeolpyo(found)


def _sorted_byeolpyo(tokens: set[str]) -> list[str]:
    """Sort 별표 tokens numerically: 2 < 7 < 7의2 < 12."""
    def key(tok: str) -> tuple[int, int]:
        if "의" in tok:
            b, e = tok.split("의", 1)
            return int(b), int(e)
        return int(tok), 0
    return sorted(tokens, key=key)


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class IndexedArticle:
    """One article of one markdown file, with delegation/back-ref signals."""

    file_type: str          # 법률 / 시행령 / 시행규칙 / ...
    doc_id: str             # ChromaDB doc id (raw, 의M stripped — matches ingest)
    canonical: str          # human-readable ref, 의M restored — e.g. 제28조의8
    raw_number: str         # bare heading number, 의M stripped — e.g. 제28조
    title: str              # e.g. "(개인정보의 국외 이전)"
    # delegation signals (primary law only, but harmless on decree files)
    delegates_presidential: bool = False
    delegates_ministerial: bool = False
    # 별표/별지 tokens with the 의M suffix preserved — "2", "7의2", ...
    byeolpyo_refs: list[str] = field(default_factory=list)
    byeolji_refs: list[str] = field(default_factory=list)
    # back-references (decree files): list of (base, eui, hang) tuples
    law_backrefs: list[tuple[int, int, int | None]] = field(default_factory=list)
    decree_backrefs: list[tuple[int, int, int | None]] = field(default_factory=list)


@dataclass
class DelegationChain:
    """A primary-law article and what it delegates down to."""

    law_article: str                       # canonical primary-law ref
    law_doc_id: str
    law_title: str
    delegation_kind: list[str]              # e.g. ["대통령령", "별표"]
    decree_articles: list[dict] = field(default_factory=list)   # 시행령 links
    rule_articles: list[dict] = field(default_factory=list)     # 시행규칙 links
    byeolpyo: list[str] = field(default_factory=list)           # 별표 tokens


# ─────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ─────────────────────────────────────────────────────────────────────────────


def _strip_buchik(text: str) -> str:
    """Drop the 부칙(附則) section: everything from the first 부칙 heading on."""
    m = _BUCHIK_RE.search(text)
    return text[: m.start()] if m else text


def _restore_eui(numbers: list[str]) -> list[str]:
    """
    Positionally restore the 의M suffix on a sequence of bare 제N조 numbers.

    legalize-kr strips 의M from headings, so 제7조 / 제7조의2 / 제7조의3 all
    appear as bare "제7조". Korean statutes place 의-articles right after their
    base article in order, so the k-th consecutive identical "제N조" is
    제N조 for k==1, 제N조의2 for k==2, ...  Mirrors article_lookup.py.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for num in numbers:
        seen[num] = seen.get(num, 0) + 1
        k = seen[num]
        out.append(num if k == 1 else f"{num}의{k}")
    return out


def _doc_id_for(
    law_id: str, folder_slug: str, file_type: str, raw_number: str,
    used: set[str],
) -> str:
    """
    Reproduce the doc_id scheme of ingest_legalize_kr._law_docs:
        base = f"{law_id}_{folder_slug}_{file_type}_{raw_number}"
    On collision (same raw 제N조 appears again — i.e. an 의M article) append
    _2, _3, ...  ``used`` is mutated to track ids already emitted.
    """
    base = f"{law_id}_{folder_slug}_{file_type}_{raw_number}"
    if base not in used:
        used.add(base)
        return base
    suffix = 2
    while f"{base}_{suffix}" in used:
        suffix += 1
    doc_id = f"{base}_{suffix}"
    used.add(doc_id)
    return doc_id


def _index_file(md_path: Path, is_primary: bool) -> tuple[list[IndexedArticle], dict]:
    """
    Parse one markdown file into a list of IndexedArticle, with delegation /
    back-reference signals filled in. Returns (articles, frontmatter_meta).
    """
    text = md_path.read_text(encoding="utf-8")
    meta = _parse_frontmatter(text)
    body = _strip_buchik(text)

    file_type = md_path.stem  # 법률 / 시행령 / 시행규칙 / ...
    law_id = meta.get("법령ID", "") or f"folder_{hash(md_path.parent.name) & 0xFFFFFF:06x}"
    folder_slug = md_path.parent.name[:20].replace(" ", "_")

    raw_articles = _split_articles(body)
    raw_numbers = [a.number for a in raw_articles]
    canon_numbers = _restore_eui(raw_numbers)

    used_ids: set[str] = set()
    indexed: list[IndexedArticle] = []
    for art, canon in zip(raw_articles, canon_numbers):
        doc_id = _doc_id_for(law_id, folder_slug, file_type, art.number, used_ids)
        ia = IndexedArticle(
            file_type=file_type,
            doc_id=doc_id,
            canonical=canon,
            raw_number=art.number,
            title=art.title,
        )
        if is_primary:
            ia.delegates_presidential = bool(_DELEG_PRESIDENTIAL_RE.search(art.content))
            ia.delegates_ministerial = bool(_DELEG_MINISTERIAL_RE.search(art.content))
            ia.byeolpyo_refs = _byeolpyo_tokens(art.content)
            ia.byeolji_refs = _byeolji_tokens(art.content)
        else:
            # decree file: extract back-references
            for m in _BACKREF_LAW_RE.finditer(art.content):
                base = int(m.group(1))
                eui = int(m.group(2)) if m.group(2) else 1
                hang = int(m.group(3)) if m.group(3) else None
                ia.law_backrefs.append((base, eui, hang))
            for m in _BACKREF_DECREE_RE.finditer(art.content):
                base = int(m.group(1))
                eui = int(m.group(2)) if m.group(2) else 1
                hang = int(m.group(3)) if m.group(3) else None
                ia.decree_backrefs.append((base, eui, hang))
            # also detect 별표 refs inside the decree (for chain completeness)
            ia.byeolpyo_refs = _byeolpyo_tokens(art.content)
            ia.byeolji_refs = _byeolji_tokens(art.content)
        indexed.append(ia)

    return indexed, meta


# ─────────────────────────────────────────────────────────────────────────────
# Index assembly
# ─────────────────────────────────────────────────────────────────────────────


def _canonical_to_articles(articles: list[IndexedArticle]) -> dict[str, IndexedArticle]:
    """Map canonical ref (제28조의8) -> IndexedArticle for fast lookup."""
    return {a.canonical: a for a in articles}


def _article_brief(a: IndexedArticle) -> dict:
    """Compact dict for an article inside a chain."""
    return {
        "doc_id": a.doc_id,
        "article": a.canonical,
        "title": a.title,
        "file_type": a.file_type,
    }


def _licbyl_cache_path() -> Path:
    """Sidecar cache directory for licbyl responses (one file per law name)."""
    return Path(__file__).resolve().parent / "cache" / "licbyl"


def _parse_licbyl_xml(raw: str) -> tuple[list[dict], str | None]:
    """Parse a licbyl SEARCH XML payload into catalogue entries."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return [], f"licbyl XML 파싱 실패: {exc}"

    if (root.findtext("resultCode") or "").strip() not in ("00", ""):
        return [], f"licbyl resultMsg: {root.findtext('resultMsg')}"

    entries: list[dict] = []
    for el in root.findall("licbyl"):
        g = lambda t: (el.findtext(t) or "").strip()  # noqa: E731
        name = g("별표명")
        attached = ""
        m = _BYEOLPYO_ATTACH_RE.search(name)
        if m:
            base, eui = m.group(1), m.group(2)
            attached = f"제{base}조" if not eui else f"제{base}조의{eui}"
        raw_no = g("별표번호")
        entries.append({
            "별표번호": raw_no,
            # canonical token decoded from the 6-digit code — lines up with
            # the corpus body's "별표 N(의M)" reference.
            "별표": _decode_licbyl_no(raw_no),
            "별표종류": g("별표종류"),
            "별표명": name,
            "관련법령명": g("관련법령명"),
            "관련법령ID": g("관련법령ID"),
            "별표일련번호": g("별표일련번호"),
            "attached_article": attached,
        })
    return entries, None


def fetch_byeolpyo_catalogue(
    law_name: str,
    related_law_names: list[str] | None = None,
    use_cache: bool = True,
) -> tuple[list[dict], str | None]:
    """
    Best-effort: fetch the 별표(別表) catalogue for a law from law.go.kr.

    Uses the DRF ``licbyl`` SEARCH endpoint with ``search=2`` (search by 법령명).
    Returns (entries, error). Each entry:
        {별표번호, 별표, 별표종류, 별표명, 관련법령명, 관련법령ID, 별표일련번호,
         attached_article}
      - 별표      : canonical token decoded from the 6-digit 별표번호
                    ("000702" -> "7의2"), or None if not decodable.
      - attached_article : the 제N조(의M) parsed from the "(제N조 관련)"
                    suffix in 별표명, or "" if not parseable.

    A single ``search=2`` query returns 별표 for *every* law whose name
    matches the query and across decree levels. ``related_law_names`` (the
    NFC-folded law/decree/rule display names of THIS law) filters the result
    down to entries that actually belong to this law; pass None to keep all.

    Responses are cached under ``services/crossref/cache/licbyl/`` keyed by the
    NFC-folded law name — each law is fetched from law.go.kr at most once.
    Between live HTTP calls the function sleeps ``_LICBYL_SLEEP`` seconds so
    the full 2,302-law expansion stays within law.go.kr's rate limit.

    The 별표 *body* text is NOT fetched — law.go.kr serves it only as an
    HWP/image attachment behind a JS page, not as DRF API data.
    """
    folded = _norm_law_name(law_name)
    cache_file = _licbyl_cache_path() / f"{folded}.xml"

    raw: str | None = None
    if use_cache and cache_file.exists():
        try:
            raw = cache_file.read_text(encoding="utf-8")
        except OSError:
            raw = None

    if raw is None:
        query = urllib.parse.quote(folded)
        url = (
            f"{_DRF_LICBYL_SEARCH}?OC={_DRF_OC}&target=licbyl&type=XML"
            f"&search=2&query={query}&display=100"
        )
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return [], f"licbyl API 호출 실패: {exc}"
        finally:
            # polite delay after every live call (skipped on cache hit)
            time.sleep(_LICBYL_SLEEP)
        if use_cache:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(raw, encoding="utf-8")
            except OSError:
                pass  # caching is best-effort; a write failure is non-fatal

    entries, err = _parse_licbyl_xml(raw)
    if err:
        return [], err

    if related_law_names:
        wanted = {_norm_law_name(n) for n in related_law_names if n}
        entries = [
            e for e in entries if _norm_law_name(e["관련법령명"]) in wanted
        ]
    return entries, None


def build_law_index(
    law_folder: str,
    fetch_byeolpyo: bool = False,
    use_cache: bool = True,
) -> dict:
    """
    Build the delegation cross-reference index for ONE law folder.

    Args:
        law_folder: folder name under the legalize-kr corpus, e.g.
                    "개인정보보호법" (spaces tolerated; NFC-folded).
        fetch_byeolpyo: query law.go.kr licbyl for the 별표 catalogue.
        use_cache: reuse a cached licbyl response if present (default True).

    Returns:
        A JSON-serializable dict — the sidecar index for this law.
    """
    target = unicodedata.normalize("NFC", law_folder).replace(" ", "").strip()
    law_dir: Path | None = None
    direct = _CORPUS_PATH / target
    if direct.is_dir():
        law_dir = direct
    else:
        for entry in _CORPUS_PATH.iterdir():
            if entry.is_dir() and (
                unicodedata.normalize("NFC", entry.name).replace(" ", "") == target
            ):
                law_dir = entry
                break
    if law_dir is None:
        raise FileNotFoundError(
            f"법령 폴더를 찾을 수 없습니다: '{law_folder}' (corpus: {_CORPUS_PATH})"
        )

    primary_md = law_dir / "법률.md"
    if not primary_md.exists():
        raise FileNotFoundError(f"법률.md 없음: {law_dir}")
    decree_md = law_dir / "시행령.md"
    rule_md = law_dir / "시행규칙.md"

    primary_articles, primary_meta = _index_file(primary_md, is_primary=True)
    decree_articles: list[IndexedArticle] = []
    rule_articles: list[IndexedArticle] = []
    decree_meta: dict = {}
    rule_meta: dict = {}
    if decree_md.exists():
        decree_articles, decree_meta = _index_file(decree_md, is_primary=False)
    if rule_md.exists():
        rule_articles, rule_meta = _index_file(rule_md, is_primary=False)

    primary_by_canon = _canonical_to_articles(primary_articles)
    decree_by_canon = _canonical_to_articles(decree_articles)

    # Build: primary-law canonical -> list of decree articles back-referencing it.
    # We match on (base, eui), ignoring the 제K항 granularity for the chain link.
    decree_for_law: dict[str, list[IndexedArticle]] = {}
    for d in decree_articles:
        for base, eui, _hang in d.law_backrefs:
            ref = f"제{base}조" if eui == 1 else f"제{base}조의{eui}"
            decree_for_law.setdefault(ref, []).append(d)

    # 시행규칙 -> 법: which 법률 article each 시행규칙 article points to.
    rule_for_law: dict[str, list[IndexedArticle]] = {}
    # 시행규칙 -> 영: which 시행령 article each 시행규칙 article points to.
    rule_for_decree: dict[str, list[IndexedArticle]] = {}
    for r in rule_articles:
        for base, eui, _hang in r.law_backrefs:
            ref = f"제{base}조" if eui == 1 else f"제{base}조의{eui}"
            rule_for_law.setdefault(ref, []).append(r)
        for base, eui, _hang in r.decree_backrefs:
            ref = f"제{base}조" if eui == 1 else f"제{base}조의{eui}"
            rule_for_decree.setdefault(ref, []).append(r)

    # Assemble chains for every primary-law article that delegates downward.
    chains: list[DelegationChain] = []
    for pa in primary_articles:
        linked_decree = decree_for_law.get(pa.canonical, [])
        linked_rule = rule_for_law.get(pa.canonical, [])
        kinds: list[str] = []
        if pa.delegates_presidential:
            kinds.append("대통령령")
        if pa.delegates_ministerial:
            kinds.append("총리령·부령")
        if pa.byeolpyo_refs:
            kinds.append("별표")
        # Only emit a chain if there is a delegation signal OR a real back-link.
        if not (kinds or linked_decree or linked_rule):
            continue

        # de-dup linked articles by doc_id (an article may back-ref the same
        # 법 조 in multiple 항)
        seen_d: set[str] = set()
        decree_briefs: list[dict] = []
        for d in linked_decree:
            if d.doc_id in seen_d:
                continue
            seen_d.add(d.doc_id)
            brief = _article_brief(d)
            # if this 시행령 article is itself pointed at by a 시행규칙 article,
            # chain the third tier under it
            sub_rules = rule_for_decree.get(d.canonical, [])
            if sub_rules:
                seen_sr: set[str] = set()
                brief["rule_via_decree"] = [
                    _article_brief(sr) for sr in sub_rules
                    if not (sr.doc_id in seen_sr or seen_sr.add(sr.doc_id))
                ]
            decree_briefs.append(brief)

        seen_r: set[str] = set()
        rule_briefs = [
            _article_brief(r) for r in linked_rule
            if not (r.doc_id in seen_r or seen_r.add(r.doc_id))
        ]

        chains.append(
            DelegationChain(
                law_article=pa.canonical,
                law_doc_id=pa.doc_id,
                law_title=pa.title,
                delegation_kind=kinds,
                decree_articles=decree_briefs,
                rule_articles=rule_briefs,
                byeolpyo=pa.byeolpyo_refs,
            )
        )

    # Decree-side 별표 references (aggregate, for the byeolpyo branch).
    decree_byeolpyo: dict[str, list[int]] = {}
    for d in decree_articles:
        if d.byeolpyo_refs:
            decree_byeolpyo[d.canonical] = d.byeolpyo_refs

    # 별표 catalogue (best-effort, law.go.kr licbyl) — body text not fetched.
    byeolpyo_catalogue: list[dict] = []
    byeolpyo_error: str | None = "별표 조회 안 함 (--byeolpyo 미지정)"
    if fetch_byeolpyo:
        law_display = primary_meta.get("제목", law_dir.name)
        # restrict the licbyl result to this law's own 법률/시행령/시행규칙 —
        # search=2 returns 별표 from every law whose name matches the query.
        related = [
            primary_meta.get("제목", ""),
            decree_meta.get("제목", ""),
            rule_meta.get("제목", ""),
        ]
        byeolpyo_catalogue, byeolpyo_error = fetch_byeolpyo_catalogue(
            law_display,
            related_law_names=[r for r in related if r],
            use_cache=use_cache,
        )

    index = {
        "schema": "kolaw-crossref/v1",
        "law_folder": law_dir.name,
        "law_name": primary_meta.get("제목", law_dir.name),
        "law_id": primary_meta.get("법령ID", ""),
        "enforcement_date": primary_meta.get("시행일자", "").replace("-", ""),
        "files": {
            "법률": str(primary_md),
            "시행령": str(decree_md) if decree_md.exists() else None,
            "시행규칙": str(rule_md) if rule_md.exists() else None,
        },
        "counts": {
            "law_articles": len(primary_articles),
            "decree_articles": len(decree_articles),
            "rule_articles": len(rule_articles),
            "delegation_chains": len(chains),
            "law_articles_delegating_presidential": sum(
                1 for a in primary_articles if a.delegates_presidential
            ),
            "law_articles_delegating_ministerial": sum(
                1 for a in primary_articles if a.delegates_ministerial
            ),
            "law_byeolpyo_refs": _sorted_byeolpyo(
                {n for a in primary_articles for n in a.byeolpyo_refs}
            ),
            "decree_byeolpyo_refs": _sorted_byeolpyo(
                {n for nums in decree_byeolpyo.values() for n in nums}
            ),
        },
        "delegation_chains": [
            {
                "law_article": c.law_article,
                "law_doc_id": c.law_doc_id,
                "law_title": c.law_title,
                "delegation_kind": c.delegation_kind,
                "byeolpyo": c.byeolpyo,
                "decree_articles": c.decree_articles,
                "rule_articles": c.rule_articles,
            }
            for c in chains
        ],
        "decree_byeolpyo_refs": decree_byeolpyo,
        # 별표 catalogue from law.go.kr licbyl (best-effort): number, name and
        # the article each 별표 is attached to. Body text is NOT included —
        # law.go.kr serves 별표 bodies only as HWP/image attachments.
        "byeolpyo_catalogue": byeolpyo_catalogue,
        "byeolpyo_error": byeolpyo_error,
    }
    return index


def _index_dir() -> Path:
    """Default output directory for sidecar index JSON files."""
    return Path(__file__).resolve().parent / "index"


def _build_one_law(
    law_folder: str,
    fetch_byeolpyo: bool,
    use_cache: bool,
    out_path: Path,
) -> dict:
    """Build the index for one law folder and write it to ``out_path``."""
    index = build_law_index(
        law_folder,
        fetch_byeolpyo=fetch_byeolpyo,
        use_cache=use_cache,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return index


def _print_law_summary(index: dict, out_path: Path) -> None:
    """Print the per-law summary block (single-law mode)."""
    c = index["counts"]
    print(f"[crossref] {index['law_name']} ({index['law_folder']})")
    print(f"  법률 {c['law_articles']}조 / 시행령 {c['decree_articles']}조 "
          f"/ 시행규칙 {c['rule_articles']}조")
    print(f"  위임 체인 {c['delegation_chains']}개 "
          f"(대통령령 위임 {c['law_articles_delegating_presidential']}조, "
          f"총리령·부령 위임 {c['law_articles_delegating_ministerial']}조)")
    print(f"  별표 참조 — 본법 {c['law_byeolpyo_refs']} / 시행령 {c['decree_byeolpyo_refs']}")
    cat = index["byeolpyo_catalogue"]
    if cat:
        print(f"  별표 목록 (law.go.kr licbyl): {len(cat)}건")
        for b in cat:
            tok = b.get("별표") or f"코드 {b['별표번호']}"
            print(f"    [{b['별표종류']} {tok}] {b['별표명']} "
                  f"→ {b['attached_article'] or '(조문 미파싱)'} ({b['관련법령명']})")
    elif index["byeolpyo_error"]:
        print(f"  별표 목록: {index['byeolpyo_error']}")
    print(f"  -> {out_path}")


def run_all(
    fetch_byeolpyo: bool = False,
    use_cache: bool = True,
    force: bool = False,
    limit: int | None = None,
    progress_every: int = 50,
) -> int:
    """
    Batch mode: build the delegation index for every law folder in the corpus.

    Walks ``_CORPUS_PATH`` (legalize-kr ``kr/``) in sorted folder order. For
    each folder:
      - if ``index/<folder>.json`` already exists and ``force`` is False, skip
        it (resume-safe — a re-run picks up where it left off);
      - otherwise build the index and write the sidecar JSON.

    Per-law try/except isolation: one law failing never aborts the run; the
    failure is collected and the loop moves on. Folders that have no 법률.md
    (decree-only / rule-only entities — there is no primary law to delegate
    FROM) are counted separately as "skipped (no primary law)", NOT as errors.

    ``progress_every`` controls how often a ``[진행] N/TOTAL`` line is written
    to stderr. ``limit`` caps the number of folders processed (slice mode for
    validation). licbyl responses are cached per law, so ``--byeolpyo`` over
    the full corpus stays within law.go.kr's rate limit and re-runs reuse the
    cache.

    Returns a process exit code: 0 if every processed law succeeded or was a
    clean skip, 1 if at least one law raised an unexpected error.
    """
    if not _CORPUS_PATH.is_dir():
        print(f"[crossref] 코퍼스 경로 없음: {_CORPUS_PATH}", file=sys.stderr)
        return 1

    folders = sorted(
        (e for e in _CORPUS_PATH.iterdir() if e.is_dir()),
        key=lambda p: p.name,
    )
    if limit is not None:
        folders = folders[:limit]
    total = len(folders)
    out_dir = _index_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[crossref] --all 배치 시작 — 대상 {total}개 폴더 "
          f"(force={force}, byeolpyo={fetch_byeolpyo})", file=sys.stderr)

    built = 0            # newly built this run
    resumed = 0          # skipped because index JSON already present
    no_primary = 0       # skipped — folder has no 법률.md (decree-only entity)
    failed: list[tuple[str, str]] = []   # (folder, error message)
    total_chains = 0
    total_byeolpyo = 0

    for i, folder in enumerate(folders, start=1):
        if i % progress_every == 0 or i == total:
            print(f"[진행] {i}/{total} ... (생성 {built} / 스킵 {resumed} "
                  f"/ 본법없음 {no_primary} / 실패 {len(failed)})",
                  file=sys.stderr)

        out_path = out_dir / f"{folder.name}.json"
        if out_path.exists() and not force:
            resumed += 1
            continue

        try:
            index = _build_one_law(
                folder.name, fetch_byeolpyo, use_cache, out_path
            )
        except FileNotFoundError as exc:
            # No 법률.md — decree-only / rule-only entity. Expected for ~1,000
            # corpus folders; it is a clean skip, not a failure.
            if "법률.md 없음" in str(exc):
                no_primary += 1
            else:
                failed.append((folder.name, f"{type(exc).__name__}: {exc}"))
            continue
        except Exception as exc:  # noqa: BLE001 — isolate any per-law fault
            failed.append((folder.name, f"{type(exc).__name__}: {exc}"))
            continue

        built += 1
        total_chains += index["counts"]["delegation_chains"]
        total_byeolpyo += len(index["byeolpyo_catalogue"])

    print(file=sys.stderr)
    print("[crossref] --all 배치 완료", file=sys.stderr)
    print(f"  대상 폴더        {total}", file=sys.stderr)
    print(f"  신규 생성        {built}", file=sys.stderr)
    print(f"  재실행 스킵      {resumed} (index JSON 이미 존재)", file=sys.stderr)
    print(f"  본법 없음 스킵   {no_primary} (시행령·규칙 단독 폴더)", file=sys.stderr)
    print(f"  실패            {len(failed)}", file=sys.stderr)
    print(f"  총 위임 체인     {total_chains} (이번 실행 신규 생성분)",
          file=sys.stderr)
    print(f"  총 별표 카탈로그 {total_byeolpyo} (이번 실행 신규 생성분)",
          file=sys.stderr)
    if failed:
        print("  실패 목록:", file=sys.stderr)
        for name, err in failed:
            print(f"    - {name}: {err}", file=sys.stderr)

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build delegation cross-reference index for law folders."
    )
    parser.add_argument(
        "law_folder",
        nargs="?",
        default=None,
        help="법령 폴더명 (legalize-kr corpus), e.g. 개인정보보호법. "
             "--all 지정 시 생략.",
    )
    parser.add_argument(
        "-o", "--out",
        type=Path,
        default=None,
        help="출력 JSON 경로 (기본: services/crossref/index/<folder>.json). "
             "단일 법령 모드에서만 유효.",
    )
    parser.add_argument(
        "--byeolpyo",
        action="store_true",
        help="law.go.kr licbyl 에서 별표 목록 best-effort 수집 (네트워크 필요)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="licbyl 사이드카 캐시를 무시하고 항상 새로 호출",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="코퍼스 전체 법령 폴더를 일괄 색인 (배치 모드)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="--all 배치에서 이미 있는 index JSON 도 다시 생성",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="--all 배치에서 처리할 폴더 수 상한 (정렬 후 앞 N개 — 검증용 슬라이스)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=50,
        help="--all 배치 진행 로그 출력 간격 (기본 50개마다)",
    )
    args = parser.parse_args()

    if args.all:
        return run_all(
            fetch_byeolpyo=args.byeolpyo,
            use_cache=not args.no_cache,
            force=args.force,
            limit=args.limit,
            progress_every=max(1, args.progress_every),
        )

    if not args.law_folder:
        parser.error("law_folder 를 지정하거나 --all 을 사용하세요.")

    index = build_law_index(
        args.law_folder,
        fetch_byeolpyo=args.byeolpyo,
        use_cache=not args.no_cache,
    )
    out_path = args.out
    if out_path is None:
        # use the RESOLVED folder name (NFC fold / space tolerance) so the
        # default output path is stable regardless of how the arg was typed.
        out_path = _index_dir() / f"{index['law_folder']}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_law_summary(index, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
