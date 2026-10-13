#!/usr/bin/env bash
# import_postgres.sh — Restore Postgres (including pgvector embeddings) on vast.ai (or any fresh host)
# Usage:  ./scripts/import_postgres.sh [data-package-dir]
#         ./scripts/import_postgres.sh          # auto-detects ./data_package/
#         DATA_PACKAGE_URL=https://... ./scripts/import_postgres.sh   # download first

set -euo pipefail

DATA_DIR="${1:-./data_package}"

# ── Config ────────────────────────────────────────────────────────────────────
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
echo "  AI SRE Platform — Data Import"
echo "  Source:   $DATA_DIR"
echo "  Postgres: $PGHOST:$PGPORT/$PGDATABASE"
echo "═══════════════════════════════════════════════"

# ── 0. Optionally download from URL ──────────────────────────────────────────
if [ -n "${DATA_PACKAGE_URL:-}" ] && [ ! -d "$DATA_DIR" ]; then
    echo ""
    echo "▶ Downloading data package from $DATA_PACKAGE_URL ..."
    mkdir -p "$DATA_DIR"
    curl -L "$DATA_PACKAGE_URL" | tar -xz -C "$DATA_DIR"
    echo "   ✓ Downloaded."
fi

if [ ! -d "$DATA_DIR" ]; then
    echo ""
    echo "ERROR: Data directory not found: $DATA_DIR"
    echo ""
    echo "Options:"
    echo "  A) SCP from dev machine:"
    echo "     scp -r user@devmachine:/path/to/data_package/ ./data_package/"
    echo ""
    echo "  B) Download from GitHub Releases:"
    echo "     DATA_PACKAGE_URL=https://github.com/org/repo/releases/download/v1.0-data/data_package.tar.gz \\"
    echo "     ./scripts/import_postgres.sh"
    echo ""
    echo "  C) Run export first on dev machine:"
    echo "     ./scripts/export_postgres.sh"
    exit 1
fi

# ── 1. Wait for Postgres ──────────────────────────────────────────────────────
echo ""
echo "▶ Waiting for Postgres to be ready..."
MAX_WAIT=60
WAITED=0
until PGPASSWORD="$PGPASSWORD" pg_isready -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -q 2>/dev/null; do
    sleep 2
    WAITED=$((WAITED + 2))
    if [ $WAITED -ge $MAX_WAIT ]; then
        echo "   ✗ Postgres not ready after ${MAX_WAIT}s"
        exit 1
    fi
done
echo "   ✓ Postgres ready."

# ── 2. Restore Postgres dump ──────────────────────────────────────────────────
DUMP_FILE="$DATA_DIR/postgres_dump.sql.gz"
if [ -f "$DUMP_FILE" ]; then
    echo ""
    echo "▶ Restoring Postgres dump ($(du -sh "$DUMP_FILE" | cut -f1))..."

    # Drop and recreate database
    PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres \
        -c "DROP DATABASE IF EXISTS $PGDATABASE;" \
        -c "CREATE DATABASE $PGDATABASE OWNER $PGUSER;" 2>/dev/null || true

    # Restore
    gunzip -c "$DUMP_FILE" | PGPASSWORD="$PGPASSWORD" psql \
        -h "$PGHOST" -p "$PGPORT" \
        -U "$PGUSER" "$PGDATABASE" \
        --quiet 2>/dev/null || true

    echo "   ✓ Postgres restored."
else
    echo "   ⚠ No postgres_dump.sql.gz found — creating schema from SQL init file..."
    SQL_INIT="./infra/init_aiops_tables.sql"
    if [ -f "$SQL_INIT" ]; then
        PGPASSWORD="$PGPASSWORD" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDATABASE" \
            -f "$SQL_INIT" --quiet 2>/dev/null || true
        echo "   ✓ Schema initialised from $SQL_INIT"
    fi
fi

# ── 3. Vectors ────────────────────────────────────────────────────────────────
# Embeddings are stored in PostgreSQL (pgvector) and arrive with the dump. The target database must run
# the pgvector image; rows without a vector can be embedded with: python pg_vector_store.py --backfill

# ── 4. Restore RAG processed outputs ─────────────────────────────────────────
RAG_ARCHIVE="$DATA_DIR/rag_data.tar.gz"
if [ -f "$RAG_ARCHIVE" ]; then
    echo ""
    echo "▶ Restoring RAG processed outputs ($(du -sh "$RAG_ARCHIVE" | cut -f1))..."
    mkdir -p data/rag
    tar -xzf "$RAG_ARCHIVE" -C .
    echo "   ✓ data/rag/ restored — semantic retrieval fully operational."
else
    echo ""
    echo "   ⚠ No rag_data.tar.gz found — RAG search will return empty results."
    echo "     To rebuild: docker compose exec app python rag_ingestion.py"
fi

# ── 5. Restore JSON knowledge files ──────────────────────────────────────────
JSON_ARCHIVE="$DATA_DIR/knowledge_json.tar.gz"
if [ -f "$JSON_ARCHIVE" ]; then
    echo ""
    echo "▶ Restoring JSON knowledge files..."
    tar -xzf "$JSON_ARCHIVE" -C .
    echo "   ✓ Knowledge JSON files restored (candidates, executions, feedback, patterns)."
fi

# ── 5. Sync knowledge to Postgres (if Postgres was schema-only) ───────────────
if [ ! -f "$DUMP_FILE" ]; then
    echo ""
    echo "▶ Seeding knowledge base to Postgres..."
    python3 sync_all_knowledge_to_postgres.py 2>/dev/null && echo "   ✓ Knowledge synced." || echo "   ⚠ Sync skipped (backend not ready yet)."
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  Import complete."
echo ""
echo "  Start the platform:"
echo "    docker compose -f infra/docker-compose.vastai.yml up -d"
echo "═══════════════════════════════════════════════"
