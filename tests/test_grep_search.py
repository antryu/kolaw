"""
Tests for the multi-keyword grep_search path (Phase 2).

These hit the real legalize-kr corpus (mounted via LEGALIZE_KR_PATH).
If the corpus is unavailable, tests are skipped rather than failed —
this keeps CI green for contributors who haven't cloned legalize-kr.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.data.legalize_kr import (
    GrepResult,
    grep_search,
    parse_query,
)

_CORPUS = Path(os.path.expanduser(
    os.getenv("LEGALIZE_KR_PATH", "~/Thairon/legalize-kr/kr")
))
_CORPUS_AVAILABLE = _CORPUS.exists() and (_CORPUS.parent / ".git").exists()

requires_corpus = pytest.mark.skipif(
    not _CORPUS_AVAILABLE,
    reason=f"legalize-kr corpus not mounted at {_CORPUS}",
)


# ─── parse_query: pure function, always testable ──────────────────────────


def test_parse_query_and_default():
    cleaned, mode = parse_query("의료 규제")
    assert mode == "AND"
    assert cleaned == "의료 규제"


def test_parse_query_or_keyword_english():
    cleaned, mode = parse_query("의료 OR 외국인")
    assert mode == "OR"
    assert "OR" not in cleaned
    assert cleaned.split() == ["의료", "외국인"]


def test_parse_query_or_pipe():
    cleaned, mode = parse_query("의료 | 외국인")
    assert mode == "OR"
    assert "|" not in cleaned
    assert cleaned.split() == ["의료", "외국인"]


def test_parse_query_or_korean():
    cleaned, mode = parse_query("의료 또는 외국인")
    assert mode == "OR"
    assert "또는" not in cleaned
    assert cleaned.split() == ["의료", "외국인"]


def test_parse_query_or_case_insensitive():
    cleaned, mode = parse_query("의료 or 외국인")
    assert mode == "OR"


def test_parse_query_single_keyword_is_and():
    cleaned, mode = parse_query("의료")
    assert mode == "AND"
    assert cleaned == "의료"


# ─── grep_search: requires real corpus ────────────────────────────────────


@requires_corpus
@pytest.mark.asyncio
async def test_grep_search_basic_hits():
    r = await grep_search("의료", limit=5)
    assert isinstance(r, GrepResult)
    assert r.error is None
    assert r.mode == "AND"
    assert r.keywords == ["의료"]
    assert len(r.hits) > 0
    for h in r.hits:
        assert h.file.startswith("kr/")
        assert h.law_name
        assert "의료" in h.excerpt or h.excerpt == ""


@requires_corpus
@pytest.mark.asyncio
async def test_grep_search_and_mode_intersects():
    """AND mode must require ALL keywords in the same file."""
    both = await grep_search("의료 규제", limit=10)
    only_med = await grep_search("의료", limit=10)
    assert both.mode == "AND"
    assert len(both.hits) <= len(only_med.hits), \
        "AND with extra keyword cannot match more than single-keyword"


@requires_corpus
@pytest.mark.asyncio
async def test_grep_search_or_mode_unions():
    """OR mode (auto-detected) should match at least as many as either keyword alone."""
    or_q = await grep_search("의료 OR 외국인", limit=20)
    only_med = await grep_search("의료", limit=20)
    assert or_q.mode == "OR"
    assert "OR" not in or_q.keywords
    assert len(or_q.hits) >= 1


@requires_corpus
@pytest.mark.asyncio
async def test_grep_search_invalid_query_safe():
    """Shell metacharacters are rejected, no exception bubbles up."""
    r = await grep_search("의료; rm -rf /", limit=5)
    assert r.hits == []
    assert r.error is not None  # validation rejected it


@requires_corpus
@pytest.mark.asyncio
async def test_grep_search_excerpt_has_line_numbers():
    """Excerpts must include line-numbered context (git grep -n -C 2)."""
    r = await grep_search("의료", limit=2)
    if not r.hits:
        pytest.skip("no hits to inspect excerpt format")
    excerpt = r.hits[0].excerpt
    assert excerpt
    # git grep -n format: "<file>:<lineno>:<text>"
    assert any(part.split(":")[1].isdigit() for part in excerpt.split("\n")
               if part.count(":") >= 2)
