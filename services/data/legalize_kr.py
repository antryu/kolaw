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
