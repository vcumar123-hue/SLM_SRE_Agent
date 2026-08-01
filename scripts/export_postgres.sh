#!/usr/bin/env bash
# export_postgres.sh — Dump the running aiops Postgres + Qdrant snapshot for transfer to vast.ai
# Usage:  ./scripts/export_postgres.sh [output-dir]
# Output: data_package/postgres_dump.sql.gz  +  data_package/qdrant_snapshot.tar.gz

set -euo pipefail

OUTPUT_DIR="${1:-./data_package}"
mkdir -p "$OUTPUT_DIR"

# ── Config (reads from .env if present) ───────────────────────────────────────
source_env() {
    if [ -f .env ]; then
        export $(grep -v '^#' .env | grep -v '^$' | xargs)
    fi
}
source_env

PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
PGDATABASE="${PGDATABASE:-aiops}"
PGUSER="${PGUSER:-aiops_user}"
PGPASSWORD="${PGPASSWORD:-StrongPassword123}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"

echo "═══════════════════════════════════════════════"
echo "  AI SRE Platform — Data Export"
echo "  Postgres: $PGHOST:$PGPORT/$PGDATABASE"
echo "  Qdrant:   $QDRANT_URL"
echo "  Output:   $OUTPUT_DIR"
echo "═══════════════════════════════════════════════"

# ── 1. Postgres dump ──────────────────────────────────────────────────────────
echo ""
echo "▶ Dumping Postgres database..."
DUMP_FILE="$OUTPUT_DIR/postgres_dump.sql.gz"
PGPASSWORD="$PGPASSWORD" pg_dump \
    -h "$PGHOST" -p "$PGPORT" \
    -U "$PGUSER" "$PGDATABASE" \
    --no-password \
    --format=plain \
    --no-owner \
    --no-acl \
    | gzip -9 > "$DUMP_FILE"

DUMP_SIZE=$(du -sh "$DUMP_FILE" | cut -f1)
echo "   ✓ Postgres dump: $DUMP_FILE ($DUMP_SIZE)"

# ── 2. Qdrant snapshot ───────────────────────────────────────────────────────
echo ""
echo "▶ Creating Qdrant snapshots..."
QDRANT_SNAPSHOT_DIR="$OUTPUT_DIR/qdrant_snapshots"
mkdir -p "$QDRANT_SNAPSHOT_DIR"

# Get all collection names
COLLECTIONS=$(curl -s "$QDRANT_URL/collections" | python3 -c "
import sys,json
data=json.load(sys.stdin)
for c in data.get('result',{}).get('collections',[]):
    print(c['name'])
" 2>/dev/null || echo "")

if [ -z "$COLLECTIONS" ]; then
    echo "   ⚠ No Qdrant collections found (Qdrant may not be running)"
else
    for COLLECTION in $COLLECTIONS; do
        echo "   Snapshotting collection: $COLLECTION"
        # Create snapshot
        SNAPSHOT_RESPONSE=$(curl -s -X POST "$QDRANT_URL/collections/$COLLECTION/snapshots")
        SNAPSHOT_NAME=$(echo "$SNAPSHOT_RESPONSE" | python3 -c "
import sys,json
data=json.load(sys.stdin)
print(data.get('result',{}).get('name',''))
" 2>/dev/null)

        if [ -n "$SNAPSHOT_NAME" ]; then
            # Download snapshot
            curl -s "$QDRANT_URL/collections/$COLLECTION/snapshots/$SNAPSHOT_NAME" \
                -o "$QDRANT_SNAPSHOT_DIR/${COLLECTION}_${SNAPSHOT_NAME}"
            echo "   ✓ $COLLECTION → $SNAPSHOT_NAME"
        else
            echo "   ✗ Failed to snapshot $COLLECTION"
        fi
    done

    # Archive all snapshots
    QDRANT_ARCHIVE="$OUTPUT_DIR/qdrant_snapshots.tar.gz"
    tar -czf "$QDRANT_ARCHIVE" -C "$OUTPUT_DIR" qdrant_snapshots/
    rm -rf "$QDRANT_SNAPSHOT_DIR"
    QDRANT_SIZE=$(du -sh "$QDRANT_ARCHIVE" | cut -f1)
    echo "   ✓ Qdrant archive: $QDRANT_ARCHIVE ($QDRANT_SIZE)"
fi

# ── 3. Small JSON knowledge files ────────────────────────────────────────────
echo ""
echo "▶ Packaging JSON knowledge files..."
JSON_ARCHIVE="$OUTPUT_DIR/knowledge_json.tar.gz"
tar -czf "$JSON_ARCHIVE" \
    data/playbooks.json \
    data/resolutions.json \
    data/taxonomy.json \
    data/automation_registry.json \
    data/global_patterns/ \
    data/backtest/temporal_index.json \
    data/backtest/self_index_client_a.json \
    2>/dev/null || true
echo "   ✓ JSON knowledge: $JSON_ARCHIVE"

# ── 4. Manifest ───────────────────────────────────────────────────────────────
echo ""
MANIFEST="$OUTPUT_DIR/manifest.json"
python3 - <<'PY'
import json, os, datetime

output_dir = os.environ.get("OUTPUT_DIR", "./data_package")
files = {}
for f in os.listdir(output_dir):
    path = os.path.join(output_dir, f)
    if os.path.isfile(path):
        files[f] = os.path.getsize(path)

manifest = {
    "exported_at": datetime.datetime.utcnow().isoformat() + "Z",
    "files": files,
    "restore_order": ["postgres_dump.sql.gz", "qdrant_snapshots.tar.gz", "knowledge_json.tar.gz"]
}
with open(os.path.join(output_dir, "manifest.json"), "w") as fh:
    json.dump(manifest, fh, indent=2)
print(json.dumps(manifest, indent=2))
PY

echo ""
echo "═══════════════════════════════════════════════"
echo "  Export complete → $OUTPUT_DIR/"
echo ""
echo "  Next steps:"
echo "  1. Upload data_package/ to GitHub Releases:"
echo "     gh release create v1.0-data ./data_package/*.gz"
echo "  OR"
echo "  1. SCP directly to vast.ai:"
echo "     scp -r data_package/ root@<vast-ip>:<ssh-port>:/workspace/ai-slm/"
echo ""
echo "  Then on vast.ai run:  ./scripts/import_postgres.sh"
echo "═══════════════════════════════════════════════"
