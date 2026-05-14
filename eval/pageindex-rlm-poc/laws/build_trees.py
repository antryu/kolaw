"""
build_trees.py — 5 법령 PageIndex 트리 일괄 build (R&D Track #2 Day 1~2).

PoC 1차 build_tree.py 로직 재사용 + 5법령 다중 source.

산출 (laws/tree/):
- <name_id>-tree.json     # 전체 트리 + 본문
- <name_id>-tree.mermaid  # chapter 단계 시각화
- <name_id>-text-tree.txt # 장/절 outline
- <name_id>-stats.json    # nodes / articles / max_depth
- summary.json            # 5법령 통계 종합
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "tree"
OUT.mkdir(exist_ok=True, parents=True)

# allow `from laws_config import ...`
sys.path.insert(0, str(ROOT))
from laws_config import LAWS, sources_for  # noqa: E402

LEVEL_NAME = {1: "law", 2: "chapter", 3: "section", 4: "subsection", 5: "article"}


@dataclass
class TreeNode:
    id: str
    level: int
    level_name: str
    title: str
    source: str
    body: str = ""
    children: list["TreeNode"] = field(default_factory=list)


def parse_markdown_to_tree(md_text: str, source: str, root_id_prefix: str) -> TreeNode:
    head_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

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
        while stack and stack[-1].level >= depth:
            stack.pop()
        if not stack:
            stack.append(root)
        counters[depth] = counters.get(depth, 0) + 1
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


def merge_roots(roots: list[TreeNode], display_name: str, name_id: str) -> TreeNode:
    super_root = TreeNode(
        id=name_id,
        level=0,
        level_name="root",
        title=f"{display_name} (multi-source)",
        source="all",
    )
    super_root.children = roots
    return super_root


def stats(root: TreeNode) -> dict:
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
    return asdict(n)


def to_mermaid(root: TreeNode, max_depth: int = 3) -> str:
    lines = ["flowchart TD"]
    seen: set[str] = set()

    def safe_id(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", s)[:40]

    def walk(n: TreeNode, depth: int):
        if depth > max_depth:
            return
        nid = safe_id(n.id)
        if nid not in seen:
            label = n.title.replace('"', "'")[:60]
            lines.append(f'    {nid}["{label}"]')
            seen.add(nid)
        for c in n.children:
            cid = safe_id(c.id)
            if cid not in seen:
                label = c.title.replace('"', "'")[:60]
                lines.append(f'    {cid}["{label}"]')
                seen.add(cid)
            lines.append(f"    {nid} --> {cid}")
            walk(c, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


def to_text_tree(root: TreeNode, max_depth: int = 4) -> str:
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


def build_one(law: dict) -> dict:
    name_id = law["name_id"]
    display = law["display"]
    srcs = sources_for(name_id)
    if not srcs:
        return {"name_id": name_id, "error": "no sources"}

    roots: list[TreeNode] = []
    for label, path in srcs:
        text = path.read_text(encoding="utf-8")
        # use law name_id as part of root id to avoid collisions across 5 laws
        root = parse_markdown_to_tree(text, label, f"{name_id}.{label}")
        roots.append(root)

    super_root = merge_roots(roots, display, name_id)
    s = stats(super_root)

    (OUT / f"{name_id}-tree.json").write_text(
        json.dumps(to_dict(super_root), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT / f"{name_id}-tree.mermaid").write_text(
        to_mermaid(super_root, max_depth=3), encoding="utf-8"
    )
    (OUT / f"{name_id}-text-tree.txt").write_text(
        to_text_tree(super_root, max_depth=4), encoding="utf-8"
    )
    (OUT / f"{name_id}-stats.json").write_text(
        json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"name_id": name_id, "display": display, "sources": [s for s, _ in srcs], **s}


def main():
    summary = []
    for law in LAWS:
        print(f"-> building tree: {law['name_id']:12s} {law['display']}")
        info = build_one(law)
        if "error" in info:
            print(f"   ERROR {info['error']}")
        else:
            print(
                f"   nodes={info['nodes']:>6} articles={info['articles']:>5} "
                f"depth={info['max_depth']} sources={info['sources']}"
            )
        summary.append(info)
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {OUT}/summary.json + 5 law artifacts.")


if __name__ == "__main__":
    main()
