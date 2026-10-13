#!/usr/bin/env bash
# export_postgres.sh — Dump the running aiops Postgres (including its pgvector embeddings) for transfer to vast.ai
# Usage:  ./scripts/export_postgres.sh [output-dir]
# Output: data_package/postgres_dump.sql.gz

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

echo "═══════════════════════════════════════════════"
echo "  AI SRE Platform — Data Export"
echo "  Postgres: $PGHOST:$PGPORT/$PGDATABASE"
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

# ── 2. Vectors ────────────────────────────────────────────────────────────────
# Embeddings live in PostgreSQL (pgvector), so the dump above already contains them.

# ── 3. RAG processed outputs (large — output of rag_ingestion.py) ─────────────
echo ""
echo "▶ Packaging data/rag/ processed outputs..."
RAG_ARCHIVE="$OUTPUT_DIR/rag_data.tar.gz"
RAG_FILES=""
for f in data/rag/tickets.json data/rag/topology.json data/rag/index_ready.json data/rag/kb.json; do
    if [ -f "$f" ] && [ "$(wc -c < "$f")" -gt 10 ]; then
        RAG_FILES="$RAG_FILES $f"
    fi
done
if [ -n "$RAG_FILES" ]; then
    tar -czf "$RAG_ARCHIVE" $RAG_FILES 2>/dev/null || true
    RAG_SIZE=$(du -sh "$RAG_ARCHIVE" 2>/dev/null | cut -f1 || echo "?")
    echo "   ✓ RAG data: $RAG_ARCHIVE ($RAG_SIZE)"
    echo "   Files included:$RAG_FILES"
else
    echo "   ⚠ No processed RAG files found — run rag_ingestion.py first"
fi

# ── 4. Small JSON knowledge files ────────────────────────────────────────────
echo ""
echo "▶ Packaging JSON knowledge files..."
JSON_ARCHIVE="$OUTPUT_DIR/knowledge_json.tar.gz"
tar -czf "$JSON_ARCHIVE" \
    data/playbooks.json \
    data/resolutions.json \
    data/taxonomy.json \
    data/automation_registry.json \
    data/knowledge_candidates.json \
    data/automation_executions.json \
    data/feedback_learning.json \
    data/feedback_weights.json \
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
    "restore_order": ["postgres_dump.sql.gz", "knowledge_json.tar.gz"]
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
