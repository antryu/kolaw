#!/usr/bin/env bash
# weekly-backup.sh — Compress ChromaDB chroma_db/ to ~/Documents/Backup/kolaw/.
#
# Triggered by: com.kolaw.weekly-backup LaunchAgent (Sunday 03:00 local)
# Runs after weekly-update.sh (02:00 + ~30min update time).
#
# Backup location: ~/Documents/Backup/kolaw/chromadb-YYYY-MM-DD.tar.gz
# Retention: last 4 backups kept (1 month rolling window)
#
# Recovery:
#   tar -xzf ~/Documents/Backup/kolaw/chromadb-YYYY-MM-DD.tar.gz -C /tmp
#   mv /tmp/chroma_db ~/PRJs/kolaw/services/fast_search/chroma_db
#   launchctl unload ~/Library/LaunchAgents/com.user.kolaw.api.plist
#   launchctl load   ~/Library/LaunchAgents/com.user.kolaw.api.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KOLAW_ROOT="$(dirname "$SCRIPT_DIR")"
CHROMA_SRC="$KOLAW_ROOT/services/fast_search/chroma_db"
DATE=$(date +%Y-%m-%d)
BACKUP_DIR="$HOME/Documents/Backup/kolaw"
BACKUP_FILE="$BACKUP_DIR/chromadb-$DATE.tar.gz"
LOG_DIR="$KOLAW_ROOT/logs"
LOG="$LOG_DIR/weekly-backup-$DATE.log"

mkdir -p "$BACKUP_DIR" "$LOG_DIR"

{
    echo "=== kolaw weekly-backup $(date) ==="
    echo "Source: $CHROMA_SRC"
    echo "Target: $BACKUP_FILE"
    echo ""

    if [[ ! -d "$CHROMA_SRC" ]]; then
        echo "ERROR: ChromaDB source not found: $CHROMA_SRC"
        exit 1
    fi

    # Compress
    tar -czf "$BACKUP_FILE" -C "$(dirname "$CHROMA_SRC")" "$(basename "$CHROMA_SRC")"
    SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
    echo "Backup created: $BACKUP_FILE ($SIZE)"

    # Rotate — keep last 4 backups
    echo ""
    echo "Rotating: keeping last 4 backups"
    ls -t "$BACKUP_DIR"/chromadb-*.tar.gz 2>/dev/null | tail -n +5 | while read -r old; do
        echo "  Removing old backup: $old"
        rm -f "$old"
    done

    echo ""
    echo "Current backups:"
    ls -lh "$BACKUP_DIR"/chromadb-*.tar.gz 2>/dev/null || echo "  (none)"

    echo ""
    echo "=== Done $(date) ==="
} >> "$LOG" 2>&1

# Print summary for launchd stderr capture
BACKUP_SIZE=$(du -sh "$BACKUP_FILE" 2>/dev/null | cut -f1 || echo "?")
echo "weekly-backup complete: $BACKUP_FILE ($BACKUP_SIZE)"
