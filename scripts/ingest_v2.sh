#!/usr/bin/env bash
# ingest_v2.sh — Build kolaw_laws_v2 (shadow collection) from scratch.
#
# This script:
#   1. Copies v1 collection to v2 (by re-ingesting from source — ChromaDB has no copy API)
#   2. Ingests corpus_gap laws from law.go.kr DRF API
#   3. Runs smoke tests
#   4. Prints swap command when ready
#
# v2 collection is built in the SAME chroma_db directory as v1,
# just with a different collection name: kolaw_laws_v2
#
# Run time: ~2 hr (MPS embedding) for full 130K + gap laws
# Partial mode: --gap-only skips the legalize-kr re-ingest and only adds gap laws

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KOLAW_ROOT="$(dirname "$SCRIPT_DIR")"
VENV="$KOLAW_ROOT/.venv"
CHROMA_PERSIST="$KOLAW_ROOT/services/fast_search/chroma_db"
V2_COLLECTION="kolaw_laws_v2"
CORPUS="$HOME/Thairon/legalize-kr/kr"

# Corpus-gap laws to add from DRF API (not in legalize-kr)
GAP_LAWS=(
    "형법"
    "형사소송법"
)

GAP_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --gap-only) GAP_ONLY=1; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

source "$VENV/bin/activate"

echo "[v2-ingest] Starting at $(date)"
echo "[v2-ingest] Target collection: $V2_COLLECTION"
echo "[v2-ingest] Chroma persist: $CHROMA_PERSIST"

if [[ $GAP_ONLY -eq 0 ]]; then
    echo ""
    echo "[v2-ingest] Step 1: Ingest legalize-kr corpus → $V2_COLLECTION"
    echo "[v2-ingest] (This takes ~2hr on MPS; skip with --gap-only if v1 is already full)"
    KOLAW_COLLECTION="$V2_COLLECTION" \
    CHROMA_PERSIST_PATH="$CHROMA_PERSIST" \
    LEGALIZE_KR_PATH="$CORPUS" \
    python -m services.fast_search.ingest_legalize_kr \
        --corpus "$CORPUS" \
        --persist "$CHROMA_PERSIST"
    echo "[v2-ingest] legalize-kr ingest complete"
else
    echo "[v2-ingest] --gap-only: skipping legalize-kr re-ingest"
    echo "[v2-ingest] NOTE: v2 will ONLY contain gap laws — swap only after full v1 copy exists"
    echo "[v2-ingest] For production v2, omit --gap-only to get full corpus."
fi

echo ""
echo "[v2-ingest] Step 2: Ingest corpus-gap laws from law.go.kr DRF"
echo "[v2-ingest] Gap laws: ${GAP_LAWS[*]}"

for law_name in "${GAP_LAWS[@]}"; do
    echo "[v2-ingest] Fetching: $law_name"
    CHROMA_PERSIST_PATH="$CHROMA_PERSIST" \
    KOLAW_COLLECTION="$V2_COLLECTION" \
    python -m services.fast_search.ingest_drf \
        --law "$law_name" \
        --collection "$V2_COLLECTION" \
        --persist "$CHROMA_PERSIST"
    sleep 1  # polite rate limit between laws
done

echo ""
echo "[v2-ingest] Step 3: Smoke test — query v2 for 형법 정당방위"
python3 -c "
import os, sys
sys.path.insert(0, '$KOLAW_ROOT')
os.environ['CHROMA_PERSIST_PATH'] = '$CHROMA_PERSIST'

from services.fast_search.ingest_legalize_kr import get_chroma_client
client = get_chroma_client('$CHROMA_PERSIST')
try:
    coll = client.get_collection('$V2_COLLECTION')
    print(f'  v2 collection count: {coll.count()}')
    # Test 형법 §21
    results = coll.query(query_texts=['형법 정당방위 요건'], n_results=3, include=['metadatas'])
    metas = results['metadatas'][0]
    found = any('형법' in (m.get('law_name','')) for m in metas)
    print(f'  형법 정당방위 hit: {found}')
    for m in metas[:2]:
        print(f'    → {m.get(\"law_name\",\"\")} {m.get(\"article\",\"\")}')
except Exception as e:
    print(f'  ERROR: {e}')
    sys.exit(1)
"

echo ""
echo "[v2-ingest] Complete at $(date)"
echo ""
echo "To swap production to v2:"
echo "  $SCRIPT_DIR/swap_collection.sh v2"
echo ""
echo "To rollback to v1:"
echo "  $SCRIPT_DIR/swap_collection.sh v1"
