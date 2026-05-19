"""
법령 간 인용 그래프 — SQLite 적재 + 양방향 조회 (citation chunk 2).

citation_index.py 가 만든 법령별 사이드카 JSON(services/crossref/citations/)
의 인용 간선을 SQLite 한 파일로 모아, 조문 단위 양방향 조회를 색인으로
제공한다. 위임 색인(build_index/lookup)이 세로(위임)라면 이건 가로(인용)다.

- outbound(law, article): 이 조문이 인용하는 법·조문
- inbound(law, article):  이 조문을 인용하는 법·조문 (역방향)

간선 한 건 = (출발 법·조문) → (도착 법·조문), strength(준용=strong) + count.
도착이 도서관 미보유 법이면 target_law_folder 가 NULL — 간선은 보존된다.

DB: services/crossref/citation_graph.db (gitignore, ``build`` 로 재생성).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import unicodedata
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_CITATIONS_DIR = _HERE / "citations"
_DB_PATH = _HERE / "citation_graph.db"

_SCHEMA = """
CREATE TABLE citation (
    id                INTEGER PRIMARY KEY,
    src_law_folder    TEXT NOT NULL,
    src_law_name      TEXT NOT NULL,
    src_doc_id        TEXT NOT NULL,
    src_article       TEXT NOT NULL,
    src_file_type     TEXT NOT NULL,
    src_title         TEXT,
    target_law_raw    TEXT NOT NULL,
    target_law_folder TEXT,            -- NULL = 도서관 미보유
    target_file_type  TEXT,
    target_article    TEXT,            -- NULL = 법 전체 인용
    target_resolved   INTEGER NOT NULL,
    strength          TEXT NOT NULL,   -- strong(준용) | weak
    cnt               INTEGER NOT NULL
);
-- outbound: 출발 조문으로 조회
CREATE INDEX ix_cite_src      ON citation(src_law_folder, src_article);
CREATE INDEX ix_cite_src_doc  ON citation(src_doc_id);
-- inbound: 도착 조문으로 조회 (역방향)
CREATE INDEX ix_cite_tgt      ON citation(target_law_folder, target_article);
"""

_COLUMNS = (
    "src_law_folder", "src_law_name", "src_doc_id", "src_article",
    "src_file_type", "src_title", "target_law_raw", "target_law_folder",
    "target_file_type", "target_article", "target_resolved", "strength", "cnt",
)


def _nfc(s: str | None) -> str | None:
    return unicodedata.normalize("NFC", s) if s else s


# ─────────────────────────────────────────────────────────────────────────────
# 적재
# ─────────────────────────────────────────────────────────────────────────────


def _rows_from_sidecar(data: dict, fallback_folder: str) -> list[tuple]:
    """
    사이드카 dict → citation row 튜플 리스트.

    target_resolved 는 사이드카 플래그를 믿지 않고 target_law_folder 존재로
    직접 결정한다(빈 문자열은 None 으로). 잘못된 데이터(타입 오류 등)는 예외를
    던져 호출자(build_db)가 해당 사이드카만 건너뛰게 한다.
    """
    src_folder = _nfc(data.get("law_folder", "")) or fallback_folder
    src_name = data.get("law_name", "") or src_folder
    rows: list[tuple] = []
    for e in data.get("outbound", []) or []:
        folder = _nfc(e.get("target_law_folder")) or None
        rows.append((
            src_folder,
            src_name,
            e.get("src_doc_id", ""),
            e.get("src_article", ""),
            e.get("src_file_type", ""),
            e.get("src_title", ""),
            e.get("target_law_raw", ""),
            folder,
            e.get("target_file_type", ""),
            e.get("target_article"),
            1 if folder else 0,            # target_resolved = folder 존재 여부
            e.get("strength", "weak"),
            int(e.get("count", 1)),
        ))
    return rows


def build_db(db_path: Path = _DB_PATH, citations_dir: Path = _CITATIONS_DIR) -> dict:
    """
    citations/*.json 사이드카를 전부 읽어 SQLite citation 테이블로 적재한다.

    임시 파일에 새로 만든 뒤 os.replace 로 원자 교체한다 — 빌드 중 예외가 나도
    기존 정상 DB 는 보존된다. 사이드카 한 건이 깨져도(JSON·타입 오류) 그 파일만
    건너뛰고 계속한다. 반복 호출 안전(전체 재빌드).
    Returns: {laws, edges, resolved, unresolved, strong, skipped} 통계.
    """
    if not citations_dir.is_dir():
        raise FileNotFoundError(f"인용 사이드카 디렉터리 없음: {citations_dir}")

    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)
    insert_sql = (
        f"INSERT INTO citation ({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _COLUMNS)})"
    )
    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(_SCHEMA)      # 새 임시 파일 — DROP 불필요
        laws = edges = resolved = strong = skipped = 0
        for path in sorted(citations_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rows = _rows_from_sidecar(data, path.stem)
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                print(f"[citation-db] skip {path.name}: {exc}", file=sys.stderr)
                skipped += 1
                continue
            if rows:
                conn.executemany(insert_sql, rows)
            laws += 1
            edges += len(rows)
            resolved += sum(1 for r in rows if r[10])
            strong += sum(1 for r in rows if r[11] == "strong")
        conn.commit()
        stats = {
            "laws": laws,
            "edges": edges,
            "resolved": resolved,
            "unresolved": edges - resolved,
            "strong": strong,
            "skipped": skipped,
        }
    except BaseException:
        conn.close()
        tmp_path.unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(tmp_path, db_path)        # 원자 교체 — 끝까지 와야 기존 DB 갱신
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# 양방향 조회
# ─────────────────────────────────────────────────────────────────────────────


def connect(db_path: Path = _DB_PATH) -> sqlite3.Connection:
    """읽기용 커넥션 — row 를 dict 처럼 다룰 수 있게 Row factory 적용."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, where: str, params: tuple) -> list[dict]:
    # 준용(strong) 먼저. strength 를 그냥 DESC 하면 문자열정렬이라 'weak' 가
    # 'strong' 보다 앞서므로, (strength='strong') 불리언으로 정렬한다.
    sql = (
        "SELECT * FROM citation WHERE " + where +
        " ORDER BY (strength = 'strong') DESC, cnt DESC, "
        "target_law_folder, src_law_folder"
    )
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def outbound(
    conn: sqlite3.Connection, law_folder: str, article: str | None = None
) -> list[dict]:
    """이 법(또는 조문)이 *인용하는* 다른 법·조문 간선."""
    law_folder = _nfc(law_folder)
    article = _nfc(article)
    if article:
        return _rows(conn, "src_law_folder = ? AND src_article = ?",
                     (law_folder, article))
    return _rows(conn, "src_law_folder = ?", (law_folder,))


def inbound(
    conn: sqlite3.Connection, law_folder: str, article: str | None = None
) -> list[dict]:
    """이 법(또는 조문)을 *인용하는* 다른 법·조문 간선 (역방향)."""
    law_folder = _nfc(law_folder)
    article = _nfc(article)
    if article:
        return _rows(conn, "target_law_folder = ? AND target_article = ?",
                     (law_folder, article))
    return _rows(conn, "target_law_folder = ?", (law_folder,))


def outbound_by_doc_id(conn: sqlite3.Connection, doc_id: str) -> list[dict]:
    """검색·조회 hit 의 doc_id 로 outbound 간선 — chunk 3 조인용."""
    return _rows(conn, "src_doc_id = ?", (doc_id,))


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _print_edges(label: str, rows: list[dict]) -> None:
    print(f"{label} — {len(rows)}건")
    for r in rows[:25]:
        ta = r["target_article"] or "(법 전체)"
        sa = r["src_article"]
        mark = "준용" if r["strength"] == "strong" else "참조"
        if label.startswith("outbound"):
            tgt = r["target_law_folder"] or f"[미보유] {r['target_law_raw']}"
            print(f"  [{mark}] {sa} → {tgt} {ta} x{r['cnt']}")
        else:
            print(f"  [{mark}] {r['src_law_folder']} {sa} → {ta} x{r['cnt']}")
    if len(rows) > 25:
        print(f"  ... 외 {len(rows) - 25}건")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="법령 간 인용 그래프 SQLite 적재·양방향 조회 (chunk 2)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="citations/*.json → SQLite 전체 재적재")
    p_out = sub.add_parser("outbound", help="이 법이 인용하는 법")
    p_out.add_argument("law_folder")
    p_out.add_argument("article", nargs="?", default=None)
    p_in = sub.add_parser("inbound", help="이 법을 인용하는 법")
    p_in.add_argument("law_folder")
    p_in.add_argument("article", nargs="?", default=None)
    args = parser.parse_args()

    if args.cmd == "build":
        stats = build_db()
        print(f"[citation-db] 적재 완료 → {_DB_PATH}")
        print(f"  법령 {stats['laws']} / 간선 {stats['edges']} "
              f"(해소 {stats['resolved']} / 미보유 {stats['unresolved']} "
              f"/ 준용 {stats['strong']}) / 스킵 {stats['skipped']}")
        return 0

    if not _DB_PATH.exists():
        print(f"[citation-db] DB 없음 — 먼저 `build` 실행: {_DB_PATH}",
              file=sys.stderr)
        return 1
    conn = connect()
    try:
        if args.cmd == "outbound":
            _print_edges(f"outbound {args.law_folder} {args.article or ''}",
                         outbound(conn, args.law_folder, args.article))
        elif args.cmd == "inbound":
            _print_edges(f"inbound {args.law_folder} {args.article or ''}",
                         inbound(conn, args.law_folder, args.article))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
