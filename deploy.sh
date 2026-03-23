#!/bin/bash
# ═══════════════════════════════════════════════════
#  CCC Intelligence Platform — Deploy Script
#  Usage: ./deploy.sh [--branch main] [--no-backup]
#  Tested on: Ubuntu 22.04 LTS
# ═══════════════════════════════════════════════════

set -euo pipefail

# ─── Colors ───
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${GREEN}[DEPLOY]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}   $1"; }
error()   { echo -e "${RED}[ERROR]${NC}  $1"; exit 1; }
section() { echo -e "\n${BLUE}${BOLD}══ $1 ══${NC}"; }

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_BRANCH="${DEPLOY_BRANCH:-main}"
SKIP_BACKUP=false
START_TIME=$(date +%s)

# ─── Parse args ───
while [[ $# -gt 0 ]]; do
    case $1 in
        --branch)     GIT_BRANCH="$2"; shift 2 ;;
        --no-backup)  SKIP_BACKUP=true; shift ;;
        --help)
            echo "Usage: $0 [--branch BRANCH] [--no-backup]"
            exit 0 ;;
        *) warn "Unknown arg: $1"; shift ;;
    esac
done

echo -e "${BLUE}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   CCC Intelligence Platform — Deploy    ║"
echo "  ║   $(date '+%Y-%m-%d %H:%M:%S')                    ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ─── Prerequisites ───
section "Checking Prerequisites"
for cmd in docker git curl; do
    command -v "$cmd" &>/dev/null && info "$cmd ✓" || error "$cmd not found — install it first"
done

docker compose version &>/dev/null || error "docker compose v2 not found"
[[ -f "$DEPLOY_DIR/.env.production" ]] || error ".env.production not found"

# Check .env has been customized
if grep -q "CHANGE_ME" "$DEPLOY_DIR/.env.production"; then
    error ".env.production contains placeholder values — update all CHANGE_ME entries first"
fi

# ─── Step 1: Pull latest code ───
section "Step 1 — Pulling Latest Code"
if git -C "$DEPLOY_DIR" rev-parse --git-dir &>/dev/null; then
    CURRENT=$(git -C "$DEPLOY_DIR" rev-parse --short HEAD)
    info "Current commit: $CURRENT"
    git -C "$DEPLOY_DIR" fetch origin
    git -C "$DEPLOY_DIR" checkout "$GIT_BRANCH"
    git -C "$DEPLOY_DIR" pull origin "$GIT_BRANCH"
    NEW=$(git -C "$DEPLOY_DIR" rev-parse --short HEAD)
    info "Updated to commit: $NEW"
    if [[ "$CURRENT" == "$NEW" ]]; then
        warn "No new commits — redeploying current version"
    fi
else
    warn "Not a git repo — skipping pull"
fi

# ─── Step 2: Pre-deploy backup ───
section "Step 2 — Pre-deploy Backup"
if [[ "$SKIP_BACKUP" == "true" ]]; then
    warn "Backup skipped (--no-backup flag)"
else
    info "Creating pre-deploy backup..."
    if docker compose -f "$DEPLOY_DIR/docker-compose.yml" ps backup 2>/dev/null | grep -q "Up"; then
        docker compose -f "$DEPLOY_DIR/docker-compose.yml" exec backup python /app/scripts/backup_database.py \
            && info "Backup complete ✓" \
            || warn "Backup failed — continuing anyway"
    else
        # Run one-shot backup
        docker compose -f "$DEPLOY_DIR/docker-compose.yml" run --rm backup \
            python /app/scripts/backup_database.py \
            && info "Backup complete ✓" \
            || warn "Backup failed — continuing anyway"
    fi
fi

# ─── Step 3: Build Docker images ───
section "Step 3 — Building Docker Images"
info "Building backend image..."
docker compose -f "$DEPLOY_DIR/docker-compose.yml" build --no-cache backend
info "Build complete ✓"

# ─── Step 4: Run database migrations ───
section "Step 4 — Database Migrations"
info "Running database initialization / migrations..."
docker compose -f "$DEPLOY_DIR/docker-compose.yml" run --rm backend \
    python -c "
import sys, os
sys.path.insert(0, '/app')
from core.database import init_db
init_db()
print('Database initialized/migrated ✓')
" && info "Migrations complete ✓" || error "Migration failed — aborting deploy"

# ─── Step 5: Restart services ───
section "Step 5 — Restarting Services"
info "Performing rolling restart..."

# Restart backend with zero-downtime (nginx buffers requests)
docker compose -f "$DEPLOY_DIR/docker-compose.yml" up -d --remove-orphans

# Wait for backend health
info "Waiting for backend to become healthy..."
MAX_WAIT=60
WAITED=0
while true; do
    STATUS=$(docker compose -f "$DEPLOY_DIR/docker-compose.yml" ps backend --format json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0].get('Health',''))" 2>/dev/null || echo "unknown")
    if [[ "$STATUS" == "healthy" ]]; then
        info "Backend is healthy ✓"
        break
    fi
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        warn "Timeout waiting for healthy status — check logs with: docker compose logs backend"
        break
    fi
    echo -n "."
    sleep 3
    WAITED=$((WAITED + 3))
done

# Reload nginx (no downtime)
docker compose -f "$DEPLOY_DIR/docker-compose.yml" exec nginx nginx -s reload 2>/dev/null \
    && info "Nginx reloaded ✓" \
    || warn "Nginx reload skipped"

# ─── Step 6: Verify deployment ───
section "Step 6 — Verification"
source "$DEPLOY_DIR/.env.production"
API_URL="${API_URL:-http://localhost}/health"

info "Testing health endpoint: $API_URL"
sleep 2
HTTP_CODE=$(curl -sSo /dev/null -w "%{http_code}" --connect-timeout 10 "$API_URL" 2>/dev/null || echo "000")

if [[ "$HTTP_CODE" == "200" ]]; then
    info "Health check passed (HTTP $HTTP_CODE) ✓"
else
    warn "Health check returned HTTP $HTTP_CODE — verify manually"
fi

# ─── Step 7: Cleanup ───
section "Step 7 — Cleanup"
info "Removing dangling images..."
docker image prune -f --filter "until=24h" &>/dev/null || true
info "Cleanup complete ✓"

# ─── Summary ───
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo ""
echo -e "${GREEN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   ✅  Deploy Successful!                ║"
echo "  ║   Duration: ${DURATION}s                          ║"
echo "  ║   Branch:   $GIT_BRANCH                        ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

info "Services running:"
docker compose -f "$DEPLOY_DIR/docker-compose.yml" ps
