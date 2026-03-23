#!/bin/bash
# ═══════════════════════════════════════════════════
#  CCC Intelligence Platform — SSL Setup
#  Uses Certbot + Let's Encrypt for free HTTPS
#  Run ONCE on fresh server after DNS is pointed
# ═══════════════════════════════════════════════════

set -euo pipefail

# ─── Colors ───
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[SSL]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ─── Load config ───
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env.production"

if [[ ! -f "$ENV_FILE" ]]; then
    error ".env.production not found at $ENV_FILE"
fi

source "$ENV_FILE"
DOMAIN="${DOMAIN_NAME:-}"
EMAIL="${SSL_EMAIL:-admin@${DOMAIN_NAME}}"

[[ -z "$DOMAIN" ]] && error "DOMAIN_NAME not set in .env.production"

info "Setting up SSL for domain: $DOMAIN"
info "Contact email: $EMAIL"

# ─── Install Certbot if not present ───
if ! command -v certbot &> /dev/null; then
    info "Installing Certbot..."
    apt-get update -q
    apt-get install -y -q certbot
fi

# ─── Create SSL directory ───
SSL_DIR="$SCRIPT_DIR/ssl"
mkdir -p "$SSL_DIR"
mkdir -p "$SCRIPT_DIR/certbot/www"

# ─── Step 1: Start nginx in HTTP-only mode for ACME challenge ───
info "Starting nginx in HTTP-only mode for domain verification..."

# Temporarily use HTTP-only nginx config
cat > /tmp/nginx_certbot.conf << EOF
events { worker_connections 1024; }
http {
    server {
        listen 80;
        server_name $DOMAIN www.$DOMAIN;
        location /.well-known/acme-challenge/ {
            root /var/www/certbot;
        }
        location / { return 200 'OK'; }
    }
}
EOF

# Bring up nginx temporarily if not running
if docker compose -f "$SCRIPT_DIR/../docker-compose.yml" ps nginx 2>/dev/null | grep -q "Up"; then
    warn "Nginx already running — ensuring port 80 is accessible"
else
    info "Starting nginx container..."
    docker compose -f "$SCRIPT_DIR/../docker-compose.yml" up -d nginx
    sleep 3
fi

# ─── Step 2: Obtain certificate ───
info "Requesting certificate from Let's Encrypt..."
certbot certonly \
    --webroot \
    --webroot-path="$SCRIPT_DIR/certbot/www" \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    -d "$DOMAIN" \
    -d "www.$DOMAIN" \
    --non-interactive

# ─── Step 3: Copy certs to deployment/ssl ───
info "Copying certificates..."
cp /etc/letsencrypt/live/$DOMAIN/fullchain.pem "$SSL_DIR/fullchain.pem"
cp /etc/letsencrypt/live/$DOMAIN/privkey.pem   "$SSL_DIR/privkey.pem"
chmod 644 "$SSL_DIR/fullchain.pem"
chmod 600 "$SSL_DIR/privkey.pem"

# ─── Step 4: Set up auto-renewal ───
info "Setting up auto-renewal cron job..."
RENEW_SCRIPT="/usr/local/bin/ccc_renew_ssl.sh"
cat > "$RENEW_SCRIPT" << RENEW
#!/bin/bash
certbot renew --quiet --webroot --webroot-path=$SCRIPT_DIR/certbot/www
cp /etc/letsencrypt/live/$DOMAIN/fullchain.pem $SSL_DIR/fullchain.pem
cp /etc/letsencrypt/live/$DOMAIN/privkey.pem   $SSL_DIR/privkey.pem
docker compose -f $SCRIPT_DIR/../docker-compose.yml exec nginx nginx -s reload
echo "[$(date)] SSL renewed" >> /var/log/ccc_ssl_renewal.log
RENEW
chmod +x "$RENEW_SCRIPT"

# Add to crontab (runs twice daily — Let's Encrypt recommendation)
(crontab -l 2>/dev/null; echo "0 0,12 * * * $RENEW_SCRIPT") | sort -u | crontab -

info "Auto-renewal cron installed: twice daily"

# ─── Step 5: Restart nginx with SSL ───
info "Reloading nginx with SSL configuration..."
docker compose -f "$SCRIPT_DIR/../docker-compose.yml" exec nginx nginx -s reload 2>/dev/null || \
docker compose -f "$SCRIPT_DIR/../docker-compose.yml" restart nginx

echo ""
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ SSL configured successfully!${NC}"
echo -e "${GREEN}  Domain:  https://$DOMAIN${NC}"
echo -e "${GREEN}  Renewal: automatic (cron, twice daily)${NC}"
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
