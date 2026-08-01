#!/usr/bin/env bash
# setup_vastai.sh — One-command bootstrap for vast.ai (or any clean Linux host)
#
# Usage:
#   1. SSH into your vast.ai instance
#   2. Upload or clone this repo:
#        git clone https://github.com/YOUR_ORG/ai-slm-platform.git /workspace/ai-slm
#   3. Run:
#        cd /workspace/ai-slm
#        cp .env.vastai.example .env
#        nano .env            # fill in GROQ_API_KEY and PGPASSWORD
#        chmod +x setup_vastai.sh && ./setup_vastai.sh

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(pwd)}"
COMPOSE_FILE="$REPO_DIR/infra/docker-compose.vastai.yml"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║      AI SRE Platform — vast.ai Setup                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── Preflight checks ──────────────────────────────────────────────────────────
check_cmd() { command -v "$1" &>/dev/null || { echo "✗ Required: $1"; exit 1; }; }
check_cmd docker
check_cmd docker compose 2>/dev/null || check_cmd "docker-compose"

if [ ! -f "$REPO_DIR/.env" ]; then
    echo "ERROR: .env not found. Copy .env.vastai.example → .env and fill in your keys."
    echo "  cp .env.vastai.example .env && nano .env"
    exit 1
fi

# Validate required vars
source "$REPO_DIR/.env"
[ -z "${GROQ_API_KEY:-}" ] && echo "ERROR: GROQ_API_KEY not set in .env" && exit 1
[ -z "${PGPASSWORD:-}" ]   && echo "ERROR: PGPASSWORD not set in .env" && exit 1

echo "✓ .env validated"

# ── Step 1: Pull / build images ───────────────────────────────────────────────
echo ""
echo "▶ Step 1/5 — Building Docker images (this takes ~5 min on first run)..."
docker compose -f "$COMPOSE_FILE" build --quiet
echo "   ✓ Images built."

# ── Step 2: Start infrastructure services first ───────────────────────────────
echo ""
echo "▶ Step 2/5 — Starting Postgres and Qdrant..."
docker compose -f "$COMPOSE_FILE" up -d postgres qdrant
echo "   Waiting 15s for services to initialise..."
sleep 15
echo "   ✓ Infrastructure services up."

# ── Step 3: Import data (Postgres dump + Qdrant snapshots) ────────────────────
echo ""
echo "▶ Step 3/5 — Importing data..."

if [ -n "${DATA_PACKAGE_URL:-}" ]; then
    echo "   Downloading data package from $DATA_PACKAGE_URL ..."
    mkdir -p "$REPO_DIR/data_package"
    curl -L "$DATA_PACKAGE_URL" | tar -xz -C "$REPO_DIR/data_package"
fi

if [ -d "$REPO_DIR/data_package" ]; then
    docker compose -f "$COMPOSE_FILE" run --rm \
        -v "$REPO_DIR/data_package:/data_package:ro" \
        app bash -c "cd /app && DATA_DIR=/data_package ./scripts/import_postgres.sh"
    echo "   ✓ Data imported."
else
    echo "   ⚠ No data_package/ found — platform will start with seed data only."
    echo "     To import later: ./scripts/import_postgres.sh"
fi

# ── Step 4: Start all services ────────────────────────────────────────────────
echo ""
echo "▶ Step 4/5 — Starting all platform services..."
docker compose -f "$COMPOSE_FILE" up -d
echo "   ✓ All services started."

# ── Step 5: Run POC seed ──────────────────────────────────────────────────────
echo ""
echo "▶ Step 5/5 — Seeding POC scenarios..."
echo "   Waiting 30s for app to be ready..."
sleep 30

MAX_WAIT=120; WAITED=0
until curl -sf "http://localhost:8001/health" > /dev/null 2>&1; do
    sleep 5; WAITED=$((WAITED + 5))
    [ $WAITED -ge $MAX_WAIT ] && echo "   ⚠ App not ready after ${MAX_WAIT}s — skipping seed" && break
done

if curl -sf "http://localhost:8001/health" > /dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" exec -T app \
        python poc/seed_poc.py --all --mode both 2>/dev/null
    echo "   ✓ POC scenarios seeded."
fi

# ── Summary ───────────────────────────────────────────────────────────────────
PUBLIC_IP=$(curl -sf http://checkip.amazonaws.com 2>/dev/null || echo "<vast-ai-public-ip>")

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Platform ready!                                    ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  UI (Streamlit):   http://$PUBLIC_IP:8501         ║"
echo "║  API (FastAPI):    http://$PUBLIC_IP:8001/docs    ║"
echo "║  API health:       http://$PUBLIC_IP:8001/health  ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Manage:  docker compose -f infra/docker-compose.vastai.yml logs -f  ║"
echo "║  Stop:    docker compose -f infra/docker-compose.vastai.yml down     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
