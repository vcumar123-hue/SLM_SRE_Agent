#!/usr/bin/env bash
# ============================================================
# vast.ai Bootstrap Script
# Run this once after launching your vast.ai instance to
# deploy the AI SRE Platform POC.
#
# Prerequisites on the vast.ai instance:
#   - Docker + docker compose installed (vast.ai base images include these)
#   - GROQ_API_KEY environment variable set (or passed via --groq-key)
#
# Usage:
#   # On your local machine, SSH to the vast.ai instance and run:
#   curl -sSL https://raw.githubusercontent.com/YOUR_ORG/ai-slm/main/setup_vastai.sh | bash
#
#   # Or upload and run:
#   scp setup_vastai.sh root@<vast-ip>:/root/
#   ssh root@<vast-ip> "bash setup_vastai.sh --groq-key gsk_xxx"
# ============================================================
set -euo pipefail

GROQ_KEY="${GROQ_API_KEY:-}"
REPO_DIR="${REPO_DIR:-/workspace/ai-slm}"
COMPOSE_FILE="infra/docker-compose.vastai.yml"
PGPASSWORD="${PGPASSWORD:-AiOpsPOC2024!}"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --groq-key) GROQ_KEY="$2"; shift 2 ;;
    --dir)      REPO_DIR="$2"; shift 2 ;;
    *) echo "Unknown: $1"; exit 1 ;;
  esac
done

echo ""
echo "══════════════════════════════════════════════════════"
echo "  AI SRE Platform — vast.ai Setup"
echo "══════════════════════════════════════════════════════"
echo "  Deploy dir : $REPO_DIR"
echo "  Compose    : $COMPOSE_FILE"
echo ""

# 1. Validate GROQ key
if [ -z "$GROQ_KEY" ]; then
  echo "ERROR: GROQ_API_KEY is required."
  echo "  Set it: export GROQ_API_KEY=gsk_... or pass --groq-key gsk_..."
  exit 1
fi

# 2. Check Docker
if ! command -v docker &>/dev/null; then
  echo "[setup] Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
docker compose version || { echo "ERROR: docker compose not available"; exit 1; }

# 3. Navigate to repo dir (assume code is already there or clone it)
if [ ! -d "$REPO_DIR" ]; then
  echo ""
  echo "ERROR: Directory $REPO_DIR not found."
  echo "  Upload or clone your AI SRE Platform code there first."
  echo "  Example: git clone <your-repo-url> $REPO_DIR"
  exit 1
fi
cd "$REPO_DIR"

# 4. Write .env
echo "[setup] Writing .env..."
cat > .env <<EOF
PRIMARY_PROVIDER=groq
GROQ_API_KEY=${GROQ_KEY}
GROQ_MODEL=llama-3.3-70b-versatile
LM_STUDIO_BASE_URL=

PGHOST=aiops-postgres
PGPORT=5432
PGDATABASE=aiops
PGUSER=aiops_user
PGPASSWORD=${PGPASSWORD}

AIOPS_DEFAULT_TENANT_ID=poc_client
AIOPS_TENANT_CONFIG_DIR=/app/tenant_config
AIOPS_API_KEY=
AIOPS_BACKEND_URL=http://aiops-app:8000
BACKEND_URL=http://aiops-app:8000

CONFIDENCE_THRESHOLD=0.48
MIN_RETRIEVAL_SCORE=0.25
FALLBACK_PROVIDER=groq

EMBEDDING_BACKEND=sentence_transformers
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DEVICE=cpu

STREAMLIT_SERVER_PORT=8501
STREAMLIT_SERVER_ADDRESS=0.0.0.0
STREAMLIT_SERVER_HEADLESS=true
STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

LOG_LEVEL=info
APP_ENV=poc
EOF
echo "[setup] .env written. ✓"

# 5. Build images
echo ""
echo "[setup] Building Docker images (this takes ~5-10 min on first run)..."
docker compose -f "$COMPOSE_FILE" build --parallel

# 6. Start services
echo ""
echo "[setup] Starting services..."
docker compose -f "$COMPOSE_FILE" up -d

# 7. Wait for backend health
echo ""
echo "[setup] Waiting for backend to become healthy..."
MAX_WAIT=180
WAITED=0
until docker compose -f "$COMPOSE_FILE" exec -T app curl -sf http://localhost:8000/health > /dev/null 2>&1; do
  if [ $WAITED -ge $MAX_WAIT ]; then
    echo "WARNING: Backend not healthy after ${MAX_WAIT}s."
    echo "  Check logs: docker compose -f $COMPOSE_FILE logs app"
    break
  fi
  echo "  ... waiting (${WAITED}s)"
  sleep 10
  WAITED=$((WAITED+10))
done

# 8. Run POC seed
echo ""
echo "[setup] Running POC seed (register CIs + seed knowledge)..."
docker compose -f "$COMPOSE_FILE" exec app python poc/seed_poc.py --setup-only --backend http://localhost:8000

# 9. Done
VAST_IP=$(curl -sf http://ifconfig.me 2>/dev/null || echo "<your-vast-ip>")

echo ""
echo "══════════════════════════════════════════════════════"
echo "  ✅ Deployment complete!"
echo ""
echo "  Streamlit UI  → http://${VAST_IP}:8501"
echo "  FastAPI docs  → http://${VAST_IP}:8001/docs"
echo ""
echo "  Next steps:"
echo "  1. Open the UI and go to 🚀 POC Console"
echo "  2. Click 'Setup' → Register CIs + Seed Knowledge"
echo "  3. Upload your ticket data (optional)"
echo "  4. Run Reactive Scenarios and Proactive Detection"
echo ""
echo "  Useful commands:"
echo "  docker compose -f $COMPOSE_FILE logs -f app    # backend logs"
echo "  docker compose -f $COMPOSE_FILE logs -f ui     # UI logs"
echo "  docker compose -f $COMPOSE_FILE ps             # service status"
echo "  docker compose -f $COMPOSE_FILE down           # stop all"
echo "══════════════════════════════════════════════════════"
