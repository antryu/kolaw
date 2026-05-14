#!/usr/bin/env python3
"""
build_tree.py — 의료법 (법률.md + 시행령.md + 시행규칙.md) → PageIndex 트리

PageIndex (vectifyai/PageIndex) 의 핵심 아이디어:
- chunk-vector RAG 대신 hierarchical tree → LLM 이 retrieval 단계에서 reasoning
- 한국 법령은 이미 "법 → 장 → 절 → 조 → 항/호" 계층이라 PDF→tree 변환 불필요
- Markdown heading depth 그대로 활용

산출:
- tree/uirobub-tree.json     # 트리 + 본문
- tree/uirobub-tree.mermaid  # 시각화 (보고서용)
- tree/uirobub-stats.json    # 깊이/노드수/조문수
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

CORPUS = Path.home() / "Thairon" / "legalize-kr" / "kr" / "의료법"
OUT = Path(__file__).parent / "tree"
OUT.mkdir(exist_ok=True, parents=True)

# Files: 법률 + 시행령 + 시행규칙 — 3 source 통합 트리
SOURCES = [
    ("법률", CORPUS / "법률.md"),
    ("시행령", CORPUS / "시행령.md"),
    ("시행규칙", CORPUS / "시행규칙.md"),
]

# heading depth → semantic level
# # = 법, ## = 장, ### = 절, #### = 관, ##### = 조
LEVEL_NAME = {1: "law", 2: "chapter", 3: "section", 4: "subsection", 5: "article"}


@dataclass
class TreeNode:
    id: str
    level: int
    level_name: str
    title: str
    source: str  # "법률" / "시행령" / "시행규칙"
    body: str = ""
    children: list["TreeNode"] = field(default_factory=list)


def parse_markdown_to_tree(md_text: str, source: str, root_id_prefix: str) -> TreeNode:
    """
    Parse Markdown headings into a hierarchical TreeNode tree.

    Approach: walk lines, maintain a stack of open nodes by depth.
    Body text accumulates under the deepest open node.
    """
    head_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    # Strip frontmatter
    if md_text.startswith("---"):
        end = md_text.find("\n---", 3)
        if end != -1:
            md_text = md_text[end + 4 :]

    root = TreeNode(
        id=root_id_prefix,
        level=0,
        level_name="root",
        title=source,
        source=source,
    )
    stack: list[TreeNode] = [root]
    body_buffer: list[str] = []
    counters: dict[int, int] = {}

    def flush_body():
        if body_buffer and len(stack) > 1:
            text = "\n".join(body_buffer).strip()
            if text:
                stack[-1].body = (stack[-1].body + "\n" + text).strip()
        body_buffer.clear()

    for line in md_text.splitlines():
        m = head_re.match(line)
        if not m:
            body_buffer.append(line)
            continue
        flush_body()
        depth = len(m.group(1))
        title = m.group(2).strip()
        # pop stack to parent depth
        while stack and stack[-1].level >= depth:
            stack.pop()
        if not stack:
            stack.append(root)
        # generate id
        counters[depth] = counters.get(depth, 0) + 1
        # reset deeper counters
        for d in list(counters):
            if d > depth:
                counters[d] = 0
        parent = stack[-1]
        node_id = f"{parent.id}.{LEVEL_NAME.get(depth, f'h{depth}')}{counters[depth]}"
        node = TreeNode(
            id=node_id,
            level=depth,
            level_name=LEVEL_NAME.get(depth, f"h{depth}"),
            title=title,
            source=source,
        )
        parent.children.append(node)
        stack.append(node)

    flush_body()
    return root


def merge_roots(roots: list[TreeNode]) -> TreeNode:
    """Merge per-source roots under one '의료법' super-root."""
    super_root = TreeNode(
        id="medlaw",
        level=0,
        level_name="root",
        title="의료법 (3-source: 법률 + 시행령 + 시행규칙)",
        source="all",
    )
    super_root.children = roots
    return super_root


def stats(root: TreeNode) -> dict:
    """Compute tree depth, node count, article count."""
    nodes = 0
    articles = 0
    max_depth = 0

    def walk(n: TreeNode, depth: int):
        nonlocal nodes, articles, max_depth
        nodes += 1
        max_depth = max(max_depth, depth)
        if n.level_name == "article":
            articles += 1
        for c in n.children:
            walk(c, depth + 1)

    walk(root, 0)
    return {"nodes": nodes, "articles": articles, "max_depth": max_depth}


def to_dict(n: TreeNode) -> dict:
    d = asdict(n)
    return d


def to_mermaid(root: TreeNode, max_depth: int = 3) -> str:
    """
    Render top-N levels as a Mermaid flowchart.
    Article-level (depth ≥ 5 from super_root) suppressed for readability.
    """
    lines = ["flowchart TD"]
    seen: set[str] = set()

    def safe_id(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", s)[:40]

    def walk(n: TreeNode, depth: int):
        if depth > max_depth:
            return
        nid = safe_id(n.id)
        if nid not in seen:
            label = n.title.replace('"', "'")
            lines.append(f'    {nid}["{label}"]')
            seen.add(nid)
        for c in n.children:
            cid = safe_id(c.id)
            label = c.title.replace('"', "'")
            if cid not in seen:
                lines.append(f'    {cid}["{label}"]')
                seen.add(cid)
            lines.append(f"    {nid} --> {cid}")
            walk(c, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


def to_text_tree(root: TreeNode, max_depth: int = 4) -> str:
    """Render compact text tree (장/절 level)."""
    lines: list[str] = []

    def walk(n: TreeNode, depth: int):
        if depth > max_depth:
            return
        prefix = "  " * depth
        marker = "" if n.level_name in ("root",) else f"[{n.level_name}] "
        lines.append(f"{prefix}{marker}{n.title}")
        for c in n.children:
            walk(c, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


def main():
    roots = []
    for source, path in SOURCES:
        if not path.exists():
            print(f"WARN: missing {path}")
            continue
        text = path.read_text(encoding="utf-8")
        root = parse_markdown_to_tree(text, source, source)
        roots.append(root)
        print(f"  loaded {source}: {len(text):,} bytes")

    super_root = merge_roots(roots)
    s = stats(super_root)
    print(f"\nTree stats: {s}")

    (OUT / "uirobub-tree.json").write_text(
        json.dumps(to_dict(super_root), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT / "uirobub-tree.mermaid").write_text(
        to_mermaid(super_root, max_depth=3), encoding="utf-8"
    )
    (OUT / "uirobub-text-tree.txt").write_text(
        to_text_tree(super_root, max_depth=4), encoding="utf-8"
    )
    (OUT / "uirobub-stats.json").write_text(
        json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote: {OUT}/uirobub-tree.json")
    print(f"Wrote: {OUT}/uirobub-tree.mermaid")
    print(f"Wrote: {OUT}/uirobub-text-tree.txt")
    print(f"Wrote: {OUT}/uirobub-stats.json")


if __name__ == "__main__":
    main()
