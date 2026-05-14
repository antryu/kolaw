"""
incremental_update.py — Weekly incremental ChromaDB update from legalize-kr git diff.

Strategy:
  1. Run `git pull` in the legalize-kr repo.
  2. Get list of changed .md files from `git diff --name-only HEAD@{1}..HEAD`.
  3. For each changed law folder:
     - Delete existing docs for that folder from the collection.
     - Re-ingest the folder (all .md files).
  4. Report: added/updated/deleted doc counts.

If legalize-kr is not a git repo (future), falls back to full re-ingest.

Collection used: KOLAW_COLLECTION env var (same as main service).

Run:
  python -m services.fast_search.incremental_update
  python -m services.fast_search.incremental_update --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CORPUS = Path(os.path.expanduser("~/Thairon/legalize-kr/kr"))
_CORPUS_ROOT = Path(os.path.expanduser("~/Thairon/legalize-kr"))
_CORPUS_PATH = Path(os.getenv("LEGALIZE_KR_PATH", str(_DEFAULT_CORPUS)))
_CHROMA_PERSIST = os.getenv(
    "CHROMA_PERSIST_PATH",
    str(Path(__file__).parent / "chroma_db"),
)
_COLLECTION_NAME = os.getenv("KOLAW_COLLECTION", "kolaw_laws_v3")
_DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_LEGALY", "")


def _git_pull(repo_root: Path) -> tuple[bool, str]:
    """
    Pull latest from origin using rebase to avoid merge-commit issues.
    legalize-kr is a read-only source — we always want remote state.
    Uses --rebase to handle divergent branches gracefully.
    Returns (success, stdout).
    """
    try:
        result = subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            # If rebase fails, try hard reset to origin/main as last resort
            logger.warning("git pull --rebase failed, trying reset to origin/main")
            reset_result = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=60,
            )
            reset_result2 = subprocess.run(
                ["git", "reset", "--hard", "origin/main"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output2 = reset_result.stdout + reset_result.stderr + reset_result2.stdout + reset_result2.stderr
            if reset_result2.returncode != 0:
                logger.warning("git reset --hard failed:\n%s", output2)
                return False, output + "\n" + output2
            return True, output2
        return True, output
    except subprocess.TimeoutExpired:
        return False, "git pull timed out"
    except Exception as exc:
        return False, str(exc)


def _get_changed_folders(repo_root: Path) -> list[Path]:
    """
    Return list of law folder Paths that changed since last pull.
    Uses git diff HEAD@{1}..HEAD to find changed files.
    Falls back to empty list on error.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD@{1}..HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("git diff failed: %s", result.stderr)
            return []

        changed_folders: set[Path] = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.endswith(".md"):
                continue
            # line format: kr/<law_folder>/<type>.md
            parts = Path(line).parts
            if len(parts) >= 2 and parts[0] == "kr":
                folder = _CORPUS_ROOT / "kr" / parts[1]
                if folder.is_dir():
                    changed_folders.add(folder)

        return sorted(changed_folders)
    except Exception as exc:
        logger.warning("Could not get changed folders: %s", exc)
        return []


def _delete_folder_docs(collection, law_folder_name: str) -> int:
    """Delete all docs for a law folder. Returns count deleted."""
    try:
        existing = collection.get(
            where={"law_folder": law_folder_name},
            include=[],
        )
        ids = existing["ids"]
        if ids:
            collection.delete(ids=ids)
            logger.info("Deleted %d docs for folder: %s", len(ids), law_folder_name)
        return len(ids)
    except Exception as exc:
        logger.warning("Could not delete docs for %s: %s", law_folder_name, exc)
        return 0


def _notify_discord(message: str) -> None:
    """POST update summary to Discord webhook if configured."""
    if not _DISCORD_WEBHOOK:
        return
    try:
        import urllib.request
        import json
        payload = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(
            _DISCORD_WEBHOOK,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                logger.warning("Discord webhook returned %d", resp.status)
    except Exception as exc:
        logger.warning("Discord notification failed: %s", exc)


def run_incremental_update(
    corpus_root: Path = _CORPUS_ROOT,
    corpus_path: Path = _CORPUS_PATH,
    persist_path: str = _CHROMA_PERSIST,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Pull legalize-kr, find changed folders, re-ingest them.

    Returns: {folders_changed, docs_deleted, docs_added}
    """
    from services.fast_search.ingest_legalize_kr import (
        _law_docs,
        get_chroma_client,
        get_embedding_function,
    )

    # --- Step 1: git pull ---
    logger.info("Pulling legalize-kr from origin...")
    if not (corpus_root / ".git").exists():
        logger.error("legalize-kr is not a git repo at %s — cannot do incremental update", corpus_root)
        return {"folders_changed": 0, "docs_deleted": 0, "docs_added": 0, "error": 1}

    success, pull_output = _git_pull(corpus_root)
    logger.info("git pull output:\n%s", pull_output)

    if "Already up to date" in pull_output or "already up to date" in pull_output.lower():
        logger.info("No changes in legalize-kr — skipping ingest")
        return {"folders_changed": 0, "docs_deleted": 0, "docs_added": 0}

    # --- Step 2: Find changed folders ---
    changed_folders = _get_changed_folders(corpus_root)
    logger.info("Changed law folders: %d", len(changed_folders))
    for f in changed_folders:
        logger.info("  Changed: %s", f.name)

    if not changed_folders:
        logger.info("No law folders changed — nothing to ingest")
        return {"folders_changed": 0, "docs_deleted": 0, "docs_added": 0}

    if dry_run:
        print(f"[dry-run] Would update {len(changed_folders)} law folders:")
        for f in changed_folders:
            print(f"  {f.name}")
        return {"folders_changed": len(changed_folders), "docs_deleted": 0, "docs_added": 0}

    # --- Step 3: Connect to ChromaDB ---
    client = get_chroma_client(persist_path)
    ef = get_embedding_function()
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef,
    )

    total_deleted = 0
    total_added = 0

    # --- Step 4: Delete + re-ingest each changed folder ---
    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict] = []

    def flush_batch() -> None:
        nonlocal total_added
        if not batch_ids:
            return
        collection.add(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
        total_added += len(batch_ids)
        batch_ids.clear()
        batch_docs.clear()
        batch_metas.clear()

    for folder in changed_folders:
        deleted = _delete_folder_docs(collection, folder.name)
        total_deleted += deleted

        for doc_id, metadata, content in _law_docs(folder):
            batch_ids.append(doc_id)
            batch_docs.append(content)
            batch_metas.append(metadata)
            if len(batch_ids) >= 128:
                flush_batch()

    flush_batch()

    result = {
        "folders_changed": len(changed_folders),
        "docs_deleted": total_deleted,
        "docs_added": total_added,
    }
    logger.info("Incremental update complete: %s", result)

    # --- Step 5: Discord notification ---
    if _DISCORD_WEBHOOK:
        changed_names = ", ".join(f.name for f in changed_folders[:5])
        if len(changed_folders) > 5:
            changed_names += f" ... (+{len(changed_folders) - 5} more)"
        msg = (
            f"[kolaw weekly-update] {len(changed_folders)} 법령 변경 반영\n"
            f"삭제: {total_deleted} docs, 추가: {total_added} docs\n"
            f"변경 법령: {changed_names}"
        )
        _notify_discord(msg)
    else:
        logger.info("DISCORD_WEBHOOK_LEGALY not set — skipping notification")

    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Weekly incremental update for kolaw ChromaDB")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, do not modify DB")
    parser.add_argument(
        "--corpus-root",
        type=Path,
        default=_CORPUS_ROOT,
        help=f"Path to legalize-kr repo root (default: {_CORPUS_ROOT})",
    )
    parser.add_argument(
        "--persist",
        default=_CHROMA_PERSIST,
        help=f"ChromaDB persist path (default: {_CHROMA_PERSIST})",
    )
    args = parser.parse_args()

    result = run_incremental_update(
        corpus_root=args.corpus_root,
        corpus_path=args.corpus_root / "kr",
        persist_path=args.persist,
        dry_run=args.dry_run,
    )
    print(f"Result: {result}")
    sys.exit(0)
