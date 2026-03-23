#!/bin/bash
# ═══════════════════════════════════════════════════
#  CCC Intelligence Platform — Server Hardening
#  Firewall · SSH · System limits · Fail2ban
#  Run ONCE on fresh Ubuntu 22.04 server as root
# ═══════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${GREEN}[SETUP]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
section() { echo -e "\n${BLUE}${BOLD}══ $1 ══${NC}"; }

# ─── Load branch IP whitelist from .env ───
ENV_FILE="$(dirname "$0")/../.env.production"
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE" || warn ".env.production not found — using defaults"

# BRANCH_IPS: space-separated list of branch office IP ranges
# Example: "192.168.1.0/24 10.10.5.0/24 78.84.12.100"
BRANCH_IPS="${BRANCH_IPS:-}"
ADMIN_SSH_PORT="${ADMIN_SSH_PORT:-22}"

echo -e "${BLUE}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   CCC — Server Hardening Setup          ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ─── Step 1: System update ───
section "System Update"
apt-get update -q
apt-get upgrade -y -q
apt-get install -y -q ufw fail2ban curl htop unzip
info "Packages installed ✓"

# ─── Step 2: UFW Firewall ───
section "Firewall (UFW)"

# Reset to defaults
ufw --force reset

# Default policy: deny everything
ufw default deny incoming
ufw default allow outgoing

# Allow SSH (with custom port support)
ufw allow "$ADMIN_SSH_PORT/tcp" comment "SSH Admin"
info "SSH port $ADMIN_SSH_PORT allowed"

# Allow HTTP and HTTPS (public — all branches connect here)
ufw allow 80/tcp  comment "HTTP (redirect to HTTPS)"
ufw allow 443/tcp comment "HTTPS — CCC Platform"
info "HTTP/HTTPS allowed"

# PostgreSQL: localhost only (Docker internal network handles this,
# but block external access explicitly)
ufw deny 5432/tcp comment "Block external PostgreSQL"
info "PostgreSQL port 5432 blocked externally"

# Optional: restrict admin panel to specific IPs
if [[ -n "$ADMIN_WHITELIST_IP" ]]; then
    ufw allow from "$ADMIN_WHITELIST_IP" to any port 443 comment "Admin IP whitelist"
    info "Admin whitelist: $ADMIN_WHITELIST_IP"
fi

# Enable firewall
ufw --force enable
info "UFW firewall enabled ✓"
ufw status verbose

# ─── Step 3: Fail2ban (brute-force protection) ───
section "Fail2ban — Brute Force Protection"

cat > /etc/fail2ban/jail.d/ccc.conf << 'JAIL'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd

[sshd]
enabled  = true
port     = ssh
logpath  = %(sshd_log)s
maxretry = 3
bantime  = 24h

[nginx-limit-req]
enabled  = true
filter   = nginx-limit-req
port     = http,https
logpath  = /var/log/nginx/error.log
maxretry = 10
bantime  = 1h

[nginx-botsearch]
enabled  = true
filter   = nginx-botsearch
port     = http,https
logpath  = /var/log/nginx/access.log
maxretry = 2
bantime  = 24h
JAIL

systemctl enable fail2ban
systemctl restart fail2ban
info "Fail2ban configured ✓"

# ─── Step 4: System limits (file descriptors, connections) ───
section "System Performance Limits"

cat >> /etc/security/limits.conf << 'LIMITS'
# CCC Platform — raise file descriptor limits
*    soft nofile 65536
*    hard nofile 65536
root soft nofile 65536
root hard nofile 65536
LIMITS

cat > /etc/sysctl.d/99-ccc.conf << 'SYSCTL'
# CCC Platform — network performance tuning
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.core.netdev_max_backlog = 5000
# Memory
vm.swappiness = 10
fs.file-max = 200000
SYSCTL

sysctl -p /etc/sysctl.d/99-ccc.conf &>/dev/null
info "System limits configured ✓"

# ─── Step 5: Create CCC system user ───
section "Service User"

if ! id -u ccc &>/dev/null; then
    useradd -r -s /sbin/nologin -d /opt/ccc -m ccc
    info "User 'ccc' created ✓"
else
    info "User 'ccc' already exists ✓"
fi

# Set permissions
mkdir -p /opt/ccc /opt/ccc/backups /opt/ccc/logs
chown -R ccc:ccc /opt/ccc
chmod 750 /opt/ccc

# ─── Step 6: Docker log rotation ───
section "Docker Log Rotation"

mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'DOCKER_DAEMON'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  },
  "live-restore": true
}
DOCKER_DAEMON

systemctl reload docker 2>/dev/null || true
info "Docker log rotation configured ✓"

# ─── Step 7: Swap (safety net for low RAM) ───
section "Swap File"

if ! swapon --show | grep -q /swapfile; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    info "2 GB swap file created ✓"
else
    info "Swap already configured ✓"
fi

# ─── Step 8: Auto-security updates ───
section "Automatic Security Updates"

apt-get install -y -q unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
info "Automatic security updates enabled ✓"

# ─── Summary ───
echo ""
echo -e "${GREEN}${BOLD}"
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   ✅  Server hardening complete!            ║"
echo "  ║                                              ║"
echo "  ║   Firewall:    UFW enabled                  ║"
echo "  ║   Port 80/443: OPEN (all branches)          ║"
echo "  ║   Port 5432:   BLOCKED (postgres safe)      ║"
echo "  ║   Fail2ban:    active                       ║"
echo "  ║   Swap:        2 GB                         ║"
echo "  ╚══════════════════════════════════════════════╝"
echo -e "${NC}"

info "Next step: cd /opt/ccc && docker compose up -d"
