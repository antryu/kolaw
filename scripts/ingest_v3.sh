#!/usr/bin/env bash
# ingest_v3.sh — Build kolaw_laws_v3: full legalize-kr corpus, all file types.
#
# Phase B.1: expands coverage from 3 fixed types (법률/시행령/시행규칙)
# to ALL .md stems in each law folder — 대통령령, 대법원규칙, 총리령,
# 각부부령, 국회규칙, 감사원규칙, 헌법, etc.
#
# Run time: ~2-3hr on MPS (MBP M4), ~4-5hr on CPU
# Collections preserved: kolaw_laws (v1) + kolaw_laws_v2 remain intact.
#
# Usage:
#   ./scripts/ingest_v3.sh              # full ingest
#   ./scripts/ingest_v3.sh --test 50   # quick smoke test (50 laws)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KOLAW_ROOT="$(dirname "$SCRIPT_DIR")"
VENV="$KOLAW_ROOT/.venv"
CHROMA_PERSIST="$KOLAW_ROOT/services/fast_search/chroma_db"
V3_COLLECTION="kolaw_laws_v3"
CORPUS="$HOME/Thairon/legalize-kr/kr"
LOG_DIR="$KOLAW_ROOT/logs"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/ingest-v3-$(date +%Y%m%d-%H%M%S).log"

TEST_ARG=""
if [[ "${1:-}" == "--test" && -n "${2:-}" ]]; then
    TEST_ARG="--test $2"
    echo "[v3-ingest] TEST MODE: $2 laws"
fi

source "$VENV/bin/activate"

echo "[v3-ingest] Starting at $(date)" | tee "$LOG"
echo "[v3-ingest] Target collection: $V3_COLLECTION" | tee -a "$LOG"
echo "[v3-ingest] Corpus: $CORPUS" | tee -a "$LOG"
echo "[v3-ingest] Log: $LOG" | tee -a "$LOG"

# --- Step 1: Count source files ---
echo "" | tee -a "$LOG"
echo "[v3-ingest] Source file distribution:" | tee -a "$LOG"
find "$CORPUS" -maxdepth 2 -name "*.md" -exec basename {} \; \
    | sed 's/\.md$//' | sort | uniq -c | sort -rn \
    | head -20 | tee -a "$LOG"
TOTAL_MD=$(find "$CORPUS" -maxdepth 2 -name "*.md" | wc -l | tr -d ' ')
echo "[v3-ingest] Total .md files: $TOTAL_MD" | tee -a "$LOG"

# --- Step 2: Ingest into v3 ---
echo "" | tee -a "$LOG"
echo "[v3-ingest] Step 2: Ingest all .md types → $V3_COLLECTION" | tee -a "$LOG"
KOLAW_COLLECTION="$V3_COLLECTION" \
CHROMA_PERSIST_PATH="$CHROMA_PERSIST" \
LEGALIZE_KR_PATH="$CORPUS" \
python -m services.fast_search.ingest_legalize_kr \
    --corpus "$CORPUS" \
    --persist "$CHROMA_PERSIST" \
    $TEST_ARG 2>&1 | tee -a "$LOG"

# --- Step 3: Smoke tests (5 canonical queries) ---
echo "" | tee -a "$LOG"
echo "[v3-ingest] Step 3: Regression smoke tests" | tee -a "$LOG"
python3 - <<PYEOF 2>&1 | tee -a "$LOG"
import os, sys
sys.path.insert(0, '$KOLAW_ROOT')
os.environ['CHROMA_PERSIST_PATH'] = '$CHROMA_PERSIST'
os.environ['KOLAW_COLLECTION'] = '$V3_COLLECTION'
os.environ['KOLAW_EMBEDDING_DEVICE'] = 'mps'

from services.fast_search.ingest_legalize_kr import get_chroma_client, get_embedding_function

client = get_chroma_client('$CHROMA_PERSIST')
ef = get_embedding_function()
try:
    coll = client.get_collection('$V3_COLLECTION', embedding_function=ef)
    print(f'  v3 collection count: {coll.count()}')
except Exception as e:
    print(f'  ERROR getting collection: {e}')
    sys.exit(1)

queries = [
    ("의료법 진료기록 보존기간", "의료법"),
    ("근로기준법 연차휴가", "근로기준법"),
    ("민법 소멸시효", "민법"),
    ("형법 정당방위", "형법"),
    ("자본시장법 공시", "자본시장"),
]

all_pass = True
for q, expected_keyword in queries:
    try:
        results = coll.query(query_texts=[q], n_results=3, include=['metadatas'])
        metas = results['metadatas'][0]
        hit = any(expected_keyword in (m.get('law_name', '') + m.get('law_folder', '')) for m in metas)
        status = 'PASS' if hit else 'MISS'
        if not hit:
            all_pass = False
        top_law = metas[0].get('law_name', '?') if metas else '?'
        top_art = metas[0].get('article', '?') if metas else '?'
        print(f"  [{status}] {q[:30]:<30} → {top_law} {top_art}")
    except Exception as e:
        print(f"  [ERR ] {q[:30]:<30} → {e}")
        all_pass = False

if all_pass:
    print("  All 5 regression tests PASS")
else:
    print("  WARNING: Some regression tests missed — review above")
PYEOF

# --- Step 4: File-type distribution in v3 ---
echo "" | tee -a "$LOG"
echo "[v3-ingest] Step 4: file_type distribution in v3 collection" | tee -a "$LOG"
python3 - <<PYEOF 2>&1 | tee -a "$LOG"
import os, sys
sys.path.insert(0, '$KOLAW_ROOT')
os.environ['CHROMA_PERSIST_PATH'] = '$CHROMA_PERSIST'

from services.fast_search.ingest_legalize_kr import get_chroma_client
from collections import Counter

client = get_chroma_client('$CHROMA_PERSIST')
coll = client.get_collection('$V3_COLLECTION')
total = coll.count()
print(f"  Total docs in v3: {total}")

# Sample 10,000 docs for distribution
batch = coll.get(limit=10000, include=['metadatas'])
counter = Counter(m.get('file_type', '?') for m in batch['metadatas'])
print("  file_type sample distribution (10k sample):")
for ft, cnt in counter.most_common():
    print(f"    {ft:<30} {cnt:>6}")
PYEOF

echo "" | tee -a "$LOG"
echo "[v3-ingest] Complete at $(date)" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "To swap production to v3:" | tee -a "$LOG"
echo "  $SCRIPT_DIR/swap_collection.sh v3" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "To rollback:" | tee -a "$LOG"
echo "  $SCRIPT_DIR/swap_collection.sh v2" | tee -a "$LOG"
