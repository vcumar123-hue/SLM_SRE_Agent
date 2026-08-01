#!/usr/bin/env bash
# ============================================================
# POC One-Click Start Script
# Runs the full 10-scenario demonstration end-to-end.
#
# Usage:
#   ./poc/start_poc.sh                    # all scenarios, both modes
#   ./poc/start_poc.sh --reactive-only    # reactive only
#   ./poc/start_poc.sh --proactive-only   # proactive only
#   ./poc/start_poc.sh --tickets /path/to/tickets.xlsx
#   ./poc/start_poc.sh --scenario 1       # single scenario
#   ./poc/start_poc.sh --speed turbo      # faster escalation
# ============================================================
set -euo pipefail

BACKEND_URL="${AIOPS_BACKEND_URL:-http://localhost:8001}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON:-python3}"

MODE="both"
SPEED="demo"
TICKETS=""
SCENARIO=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reactive-only)  MODE="reactive"; shift ;;
    --proactive-only) MODE="proactive"; shift ;;
    --speed)          SPEED="$2"; shift 2 ;;
    --tickets)        TICKETS="$2"; shift 2 ;;
    --scenario)       SCENARIO="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

echo ""
echo "═══════════════════════════════════════════════════"
echo "  AI SRE Platform — POC Demo Runner"
echo "═══════════════════════════════════════════════════"
echo "  Backend : $BACKEND_URL"
echo "  Mode    : $MODE"
echo "  Speed   : $SPEED"
echo ""

# Wait for backend
echo "[1/4] Waiting for backend to be ready..."
MAX_WAIT=120
WAITED=0
until curl -sf "$BACKEND_URL/health" > /dev/null 2>&1; do
  if [ $WAITED -ge $MAX_WAIT ]; then
    echo "ERROR: Backend not ready after ${MAX_WAIT}s. Is Docker running?"
    exit 1
  fi
  echo "  ... not ready yet (${WAITED}s elapsed)"
  sleep 5
  WAITED=$((WAITED+5))
done
echo "[1/4] Backend is ready. ✓"

# Build CLI args
SEED_ARGS="--backend $BACKEND_URL --mode $MODE --speed $SPEED"

if [ -n "$TICKETS" ]; then
  echo "[2/4] Ingesting client ticket data from: $TICKETS"
  SEED_ARGS="$SEED_ARGS --tickets $TICKETS"
else
  echo "[2/4] No ticket file provided — using POC scenario templates."
fi

if [ -n "$SCENARIO" ]; then
  SEED_ARGS="$SEED_ARGS --scenario $SCENARIO"
  echo "[3/4] Running scenario $SCENARIO..."
else
  SEED_ARGS="$SEED_ARGS --all"
  echo "[3/4] Running all 10 scenarios..."
fi

# Run the orchestrator
cd "$ROOT"
$PYTHON poc/seed_poc.py $SEED_ARGS

echo ""
echo "[4/4] POC complete."
echo ""
echo "  Open the Streamlit UI → http://localhost:8501 → 🚀 POC Console"
echo ""
echo "═══════════════════════════════════════════════════"
