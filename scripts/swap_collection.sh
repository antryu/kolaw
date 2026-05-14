#!/usr/bin/env bash
# swap_collection.sh — atomically swap kolaw ChromaDB collection v1/v2/v3
#
# Usage:
#   ./scripts/swap_collection.sh v3   # switch production to kolaw_laws_v3
#   ./scripts/swap_collection.sh v2   # rollback to kolaw_laws_v2
#   ./scripts/swap_collection.sh v1   # rollback to kolaw_laws (original)
#
# What it does:
#   1. Updates KOLAW_COLLECTION in .env (creates if absent)
#   2. Flushes BM25 cache by touching a sentinel file
#   3. Restarts kolaw uvicorn process (SIGTERM → wait → start)
#
# Safety:
#   - Does NOT delete old collection (safe rollback)
#   - Prints current state before and after swap
#   - Requires kolaw running via: uvicorn apps.api.main:app --port 8100

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KOLAW_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$KOLAW_ROOT/.env"
HEALTH_URL="http://localhost:8100/health"
PID_FILE="$KOLAW_ROOT/.kolaw.pid"

usage() {
    echo "Usage: $0 <v1|v2|v3>"
    echo "  v1 = kolaw_laws        (original production collection)"
    echo "  v2 = kolaw_laws_v2     (expanded corpus with 시행령/시행규칙)"
    echo "  v3 = kolaw_laws_v3     (Phase B.1: all file types, 대통령령/대법원규칙/부령 etc)"
    exit 1
}

if [[ $# -ne 1 ]]; then
    usage
fi

TARGET="$1"
case "$TARGET" in
    v1) COLLECTION_NAME="kolaw_laws" ;;
    v2) COLLECTION_NAME="kolaw_laws_v2" ;;
    v3) COLLECTION_NAME="kolaw_laws_v3" ;;
    *)  echo "Error: target must be 'v1', 'v2', or 'v3'"; usage ;;
esac

echo "[swap] Target collection: $COLLECTION_NAME"

# --- Step 1: Update .env ---
if [[ -f "$ENV_FILE" ]]; then
    if grep -q "^KOLAW_COLLECTION=" "$ENV_FILE"; then
        # Replace existing
        if [[ "$(uname)" == "Darwin" ]]; then
            sed -i '' "s|^KOLAW_COLLECTION=.*|KOLAW_COLLECTION=$COLLECTION_NAME|" "$ENV_FILE"
        else
            sed -i "s|^KOLAW_COLLECTION=.*|KOLAW_COLLECTION=$COLLECTION_NAME|" "$ENV_FILE"
        fi
    else
        echo "KOLAW_COLLECTION=$COLLECTION_NAME" >> "$ENV_FILE"
    fi
else
    echo "KOLAW_COLLECTION=$COLLECTION_NAME" > "$ENV_FILE"
fi
echo "[swap] Updated .env: KOLAW_COLLECTION=$COLLECTION_NAME"

# --- Step 2: Update launchd plist KOLAW_COLLECTION and reload ---
PLIST="$HOME/Library/LaunchAgents/com.user.kolaw.api.plist"
LAUNCHD_LABEL="com.user.kolaw.api"

if [[ -f "$PLIST" ]]; then
    echo "[swap] Updating plist: $PLIST"
    /usr/bin/plutil -replace "EnvironmentVariables.KOLAW_COLLECTION" \
        -string "$COLLECTION_NAME" "$PLIST"
    echo "[swap] Reloading via launchctl..."
    launchctl unload "$PLIST" 2>/dev/null || true
    sleep 2
    launchctl load "$PLIST"
    echo "[swap] launchctl reload done"
else
    echo "[swap] No launchd plist found at $PLIST — falling back to manual process management"
    KOLAW_PID=$(pgrep -f "uvicorn apps.api.main:app" 2>/dev/null | head -1 || true)
    if [[ -n "$KOLAW_PID" ]]; then
        echo "[swap] Stopping kolaw (PID=$KOLAW_PID)..."
        kill "$KOLAW_PID"
        for i in $(seq 1 10); do
            if ! kill -0 "$KOLAW_PID" 2>/dev/null; then
                echo "[swap] Process exited after ${i}s"
                break
            fi
            sleep 1
        done
        if kill -0 "$KOLAW_PID" 2>/dev/null; then
            kill -9 "$KOLAW_PID" || true
        fi
    fi
    echo "[swap] Starting kolaw with KOLAW_COLLECTION=$COLLECTION_NAME..."
    cd "$KOLAW_ROOT"
    source "$KOLAW_ROOT/.venv/bin/activate" 2>/dev/null || true
    nohup env \
        KOLAW_COLLECTION="$COLLECTION_NAME" \
        CHROMA_PERSIST_PATH="$KOLAW_ROOT/services/fast_search/chroma_db" \
        uvicorn apps.api.main:app \
            --port 8100 \
            --host 0.0.0.0 \
            --no-access-log \
        > "$KOLAW_ROOT/kolaw.log" 2>&1 &
    NEW_PID=$!
    echo "[swap] Started kolaw PID=$NEW_PID"
    echo "$NEW_PID" > "$PID_FILE"
fi

# --- Step 4: Health check ---
echo "[swap] Waiting for health check..."
for i in $(seq 1 30); do
    STATUS=$(curl -sf "$HEALTH_URL" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || true)
    if [[ "$STATUS" == "ok" || "$STATUS" == "degraded" ]]; then
        echo "[swap] Health check passed: status=$STATUS"
        echo "[swap] Swap complete: production now using $COLLECTION_NAME"
        exit 0
    fi
    sleep 1
done

echo "[swap] WARNING: Health check did not pass within 30s — check $KOLAW_ROOT/kolaw.log"
exit 1
