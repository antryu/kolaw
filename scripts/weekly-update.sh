#!/usr/bin/env bash
# weekly-update.sh — Pull legalize-kr changes and incrementally update ChromaDB.
#
# Triggered by: com.kolaw.weekly-update LaunchAgent (Sunday 02:00 local)
#
# What it does:
#   1. git pull in legalize-kr repo
#   2. Detect changed law folders
#   3. Delete+re-ingest only changed folders into current KOLAW_COLLECTION
#   4. Incremental 헌재 결정 update — fetch new DRF detc pages since last run (B.5.2)
#   5. Log results; post to Discord if DISCORD_WEBHOOK_LEGALY is set
#
# Environment:
#   KOLAW_COLLECTION           — collection to update (default: kolaw_laws_v3)
#   KOLAW_CONSTITUTIONAL_COLLECTION — 헌재 collection (default: kolaw_constitutional)
#   DISCORD_WEBHOOK_LEGALY     — optional; skip notification if unset
#   KOLAW_CONSTITUTIONAL_SKIP  — set to "1" to skip 헌재 update (e.g. initial ingest running)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KOLAW_ROOT="$(dirname "$SCRIPT_DIR")"
VENV="$KOLAW_ROOT/.venv"
LOG_DIR="$KOLAW_ROOT/logs"
LOG="$LOG_DIR/weekly-update-$(date +%Y%m%d).log"

mkdir -p "$LOG_DIR"

{
    echo "=== kolaw weekly-update $(date) ==="
    echo "Collection: ${KOLAW_COLLECTION:-kolaw_laws_v3}"
    echo ""

    # Activate venv
    source "$VENV/bin/activate"

    # --- Step 1: legalize-kr incremental update ---
    echo "--- legalize-kr incremental update ---"
    cd "$KOLAW_ROOT"
    python -m services.fast_search.incremental_update \
        --corpus-root "$HOME/Thairon/legalize-kr" \
        --persist "$KOLAW_ROOT/services/fast_search/chroma_db"

    # --- Step 2: 헌재 결정 incremental update (B.5.2) ---
    # Fetches new decisions added since last run via DRF detc.
    # Runs --max-pages 5 (500 decisions) which covers ~1 week of new decisions.
    # Skip if KOLAW_CONSTITUTIONAL_SKIP=1 (e.g. initial full ingest is running).
    echo ""
    echo "--- 헌재 결정 incremental update (B.5.2) ---"
    if [[ "${KOLAW_CONSTITUTIONAL_SKIP:-0}" == "1" ]]; then
        echo "KOLAW_CONSTITUTIONAL_SKIP=1 — skipping 헌재 update"
    else
        KOLAW_CONSTITUTIONAL_COLLECTION="${KOLAW_CONSTITUTIONAL_COLLECTION:-kolaw_constitutional}" \
        python -m services.fast_search.ingest_constitutional \
            --max-pages 5 \
            --persist "$KOLAW_ROOT/services/fast_search/chroma_db"
        echo "헌재 incremental update complete"
    fi

    echo ""
    echo "=== Done $(date) ==="
} >> "$LOG" 2>&1

# Keep last 12 weekly logs (3 months)
ls -t "$LOG_DIR"/weekly-update-*.log 2>/dev/null | tail -n +13 | xargs -I{} rm -f {}

echo "weekly-update complete — log: $LOG"
