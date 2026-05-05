"""
legalize-kr corpus loader.

Source: github.com/9bow/legalize-kr
Local mount: ~/Thairon/legalize-kr/ (251MB, 2303 statutes)

Reads Markdown files, splits on 제N조 regex, returns ArticleTree.
Phase 1: basic loader. Phase 2: deepen parsing, add metadata index.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Local corpus path — override with LEGALIZE_KR_PATH env var
_DEFAULT_PATH = Path(os.path.expanduser("~/Thairon/legalize-kr/kr"))
_CORPUS_PATH = Path(os.getenv("LEGALIZE_KR_PATH", str(_DEFAULT_PATH)))

# Regex: matches 제N조, 제N조의M, 제N조(title) patterns in Korean law Markdown
_ARTICLE_RE = re.compile(r"^#{1,6}\s*(제\d+조(?:의\d+)?(?:\s*\([^)]+\))?)", re.MULTILINE)


@dataclass
class Article:
    number: str       # e.g. "제2조"
    title: str        # e.g. "(정의)" — empty if none
    content: str      # raw Markdown text of the article


@dataclass
class ArticleTree:
    law_id: str
    law_name: str
    version: str       # enforcement_date from frontmatter, e.g. "20251001"
    source_path: str
    articles: list[Article] = field(default_factory=list)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML frontmatter fields (simple key: value, no nesting)."""
    meta: dict[str, str] = {}
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end == -1:
        return meta
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip("'\"")
    return meta


def _split_articles(text: str) -> list[Article]:
    """Split law Markdown into per-article chunks using 제N조 headings."""
    matches = list(_ARTICLE_RE.finditer(text))
    articles: list[Article] = []
    for i, match in enumerate(matches):
        heading = match.group(1)
        # Extract number and optional title
        m = re.match(r"(제\d+조(?:의\d+)?)\s*(\([^)]+\))?", heading)
        if not m:
            continue
        number = m.group(1)
        title = m.group(2) or ""
        # Content: from end of this heading to start of next
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        articles.append(Article(number=number, title=title, content=content))
    return articles


def load_law(law_name: str) -> ArticleTree | None:
    """
    Load a single law by folder name from the legalize-kr corpus.

    Args:
        law_name: Exact folder name under kr/, e.g.
                  "수소경제육성및수소안전관리에관한법률"

    Returns:
        ArticleTree or None if not found.
    """
    law_dir = _CORPUS_PATH / law_name
    if not law_dir.exists():
        return None

    # Prefer 법률.md; fall back to first .md found
    md_file = law_dir / "법률.md"
    if not md_file.exists():
        candidates = list(law_dir.glob("*.md"))
        if not candidates:
            return None
        md_file = candidates[0]

    text = md_file.read_text(encoding="utf-8")
    meta = _parse_frontmatter(text)

    law_id = meta.get("법령ID", "unknown")
    law_display_name = meta.get("제목", law_name)
    enforcement = meta.get("시행일자", "").replace("-", "")

    tree = ArticleTree(
        law_id=law_id,
        law_name=law_display_name,
        version=enforcement,
        source_path=str(md_file),
    )
    tree.articles = _split_articles(text)
    return tree


def list_available_laws() -> list[str]:
    """Return all law folder names available in the corpus."""
    if not _CORPUS_PATH.exists():
        raise FileNotFoundError(f"legalize-kr corpus not found at {_CORPUS_PATH}")
    return [d.name for d in _CORPUS_PATH.iterdir() if d.is_dir()]



# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Multi-keyword grep search
# Ported from y-company-api/src/lib/law-search.ts
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import shutil
from typing import Literal

# Allow word chars, hangul, spaces, hyphen, pipe — pipe is OR marker
_QUERY_RE = re.compile(r"^[\w\s가-힣\-|]{1,200}$")
_PATH_BLACKLIST = re.compile(r"[`$();&<>]")
_OR_RE = re.compile(r"\s+(?:OR|또는)\s+|\s*\|\s*", re.IGNORECASE)


@dataclass
class GrepHit:
    """One matching law file with line-numbered excerpt."""
    file: str             # e.g. "kr/의료법/법률.md"
    law_name: str         # e.g. "의료법"
    type: str             # 법률 / 시행령 / 시행규칙 / ...
    excerpt: str          # line-numbered context lines
    matched_keywords: list[str]


@dataclass
class GrepResult:
    query: str
    cleaned_query: str
    keywords: list[str]
    mode: Literal["AND", "OR"]
    hits: list[GrepHit]
    source: str = "legalize-kr"
    error: str | None = None


def parse_query(query: str) -> tuple[str, Literal["AND", "OR"]]:
    """
    Detect OR markers in the query string.
        "의료 OR 규제"   → ("의료 규제", "OR")
        "의료 | 규제"    → ("의료 규제", "OR")
        "의료 또는 규제" → ("의료 규제", "OR")
        "의료 규제"     → ("의료 규제", "AND")
    """
    if _OR_RE.search(query):
        return _OR_RE.sub(" ", query).strip(), "OR"
    return query.strip(), "AND"


async def grep_search(
    query: str,
    mode: Literal["AND", "OR"] | None = None,
    limit: int = 10,
) -> GrepResult:
    """
    Multi-keyword search over legalize-kr file contents via `git grep`.

    - AND (default): all keywords must appear in the same file (--all-match)
    - OR: any keyword (no --all-match)
    - Auto-detects mode from query if `mode` is None (OR markers in query)
    """
    cleaned, detected_mode = parse_query(query)
    final_mode = mode or detected_mode

    if not _QUERY_RE.match(cleaned):
        return GrepResult(query=query, cleaned_query=cleaned, keywords=[], mode=final_mode,
                          hits=[], error="Invalid query characters")

    keywords = [w for w in cleaned.split() if w]
    if not keywords:
        return GrepResult(query=query, cleaned_query=cleaned, keywords=[], mode=final_mode, hits=[])

    # Resolve corpus root (for git, we run inside the repo root, not kr/ subdir)
    repo_root = _CORPUS_PATH.parent if _CORPUS_PATH.name == "kr" else _CORPUS_PATH
    if _PATH_BLACKLIST.search(str(repo_root)):
        return GrepResult(query=query, cleaned_query=cleaned, keywords=keywords, mode=final_mode,
                          hits=[], error="Invalid corpus path")
    if not (repo_root / ".git").exists():
        return GrepResult(query=query, cleaned_query=cleaned, keywords=keywords, mode=final_mode,
                          hits=[], error=f"Not a git repo: {repo_root}")

    git_bin = shutil.which("git")
    if not git_bin:
        return GrepResult(query=query, cleaned_query=cleaned, keywords=keywords, mode=final_mode,
                          hits=[], error="git binary not found on PATH")

    # git -c core.quotepath=false grep -l [-i] [--all-match] -e KW1 -e KW2 ... -- kr/
    args = [git_bin, "-c", "core.quotepath=false", "grep", "-l", "-i"]
    if final_mode == "AND" and len(keywords) > 1:
        args.append("--all-match")
    for kw in keywords:
        args.extend(["-e", kw])
    args.extend(["--", "kr/"])

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        except asyncio.TimeoutError:
            proc.kill()
            return GrepResult(query=query, cleaned_query=cleaned, keywords=keywords, mode=final_mode,
                              hits=[], error="grep timeout")
    except Exception as exc:  # noqa: BLE001
        return GrepResult(query=query, cleaned_query=cleaned, keywords=keywords, mode=final_mode,
                          hits=[], error=f"grep failed: {exc}")

    files = [line for line in stdout.decode("utf-8", errors="replace").strip().split("\n") if line][:limit]
    if not files:
        return GrepResult(query=query, cleaned_query=cleaned, keywords=keywords, mode=final_mode, hits=[])

    hits: list[GrepHit] = []
    for f in files:
        ctx_args = [git_bin, "-c", "core.quotepath=false", "grep", "-n", "-i", "-C", "2"]
        for kw in keywords:
            ctx_args.extend(["-e", kw])
        ctx_args.extend(["--", f])
        try:
            ctx_proc = await asyncio.create_subprocess_exec(
                *ctx_args, cwd=str(repo_root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                ctx_out, _ = await asyncio.wait_for(ctx_proc.communicate(), timeout=4)
            except asyncio.TimeoutError:
                ctx_proc.kill()
                ctx_out = b""
        except Exception:  # noqa: BLE001
            ctx_out = b""
        excerpt_lines = ctx_out.decode("utf-8", errors="replace").split("\n")[:25]
        excerpt = "\n".join(excerpt_lines)[:1500]

        parts = f.split("/")
        law_name = parts[1] if len(parts) > 1 else f
        type_ = parts[2].replace(".md", "") if len(parts) > 2 else ""
        hits.append(GrepHit(file=f, law_name=law_name, type=type_,
                            excerpt=excerpt, matched_keywords=keywords))

    return GrepResult(query=query, cleaned_query=cleaned, keywords=keywords, mode=final_mode,
                      hits=hits)
