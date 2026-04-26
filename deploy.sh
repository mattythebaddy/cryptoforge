#!/bin/bash
# CryptoForge VPS Deploy Script
# Usage: ssh into your VPS, clone the repo, then run this script.
#
# Tested on: Ubuntu 22.04 / 24.04 (Hostinger, Hetzner, DigitalOcean)
#   - Minimum: 1 vCPU, 2GB RAM
#   - Recommended: 2 vCPU, 4GB RAM
#
# Steps:
#   1. SSH into your VPS:  ssh root@your-vps-ip
#   2. Clone the repo:     git clone <your-repo-url> /opt/cryptoforge
#   3. cd /opt/cryptoforge
#   4. cp .env.example .env && nano .env   (fill in your secrets)
#   5. bash deploy.sh

set -euo pipefail

VPS_IP=$(hostname -I | awk '{print $1}')

echo "============================================"
echo "   CryptoForge VPS Deploy"
echo "============================================"
echo ""

# 1. System updates
echo "[1/6] Updating system packages..."
apt-get update -qq && apt-get upgrade -y -qq

# 2. Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "[2/6] Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
else
    echo "[2/6] Docker already installed ($(docker --version | awk '{print $3}'))"
fi

# 3. Install Docker Compose plugin if not present
if ! docker compose version &> /dev/null; then
    echo "[3/6] Installing Docker Compose plugin..."
    apt-get install -y -qq docker-compose-plugin
else
    echo "[3/6] Docker Compose already installed"
fi

# 4. Check .env
if [ ! -f .env ]; then
    echo ""
    echo "ERROR: .env file not found!"
    echo "  cp .env.example .env && nano .env"
    echo ""
    echo "Required values:"
    echo "  - CRYPTOFORGE_EXCHANGE__API_KEY      (Binance testnet)"
    echo "  - CRYPTOFORGE_EXCHANGE__API_SECRET    (Binance testnet)"
    echo "  - CRYPTOFORGE_TELEGRAM__BOT_TOKEN     (optional)"
    echo "  - CRYPTOFORGE_TELEGRAM__CHAT_ID       (optional)"
    echo "  - DB_PASSWORD                         (pick any strong password)"
    exit 1
fi
echo "[4/6] .env found"

# 5. Configure firewall (allow SSH + app ports)
echo "[5/6] Configuring firewall..."
if command -v ufw &> /dev/null; then
    ufw allow 22/tcp    >/dev/null 2>&1   # SSH
    ufw allow 8050/tcp  >/dev/null 2>&1   # Dashboard
    ufw allow 3000/tcp  >/dev/null 2>&1   # Grafana
    ufw allow 9091/tcp  >/dev/null 2>&1   # Prometheus
    ufw --force enable  >/dev/null 2>&1
    echo "  Firewall: SSH(22), Dashboard(8050), Grafana(3000), Prometheus(9091)"
else
    echo "  ufw not found — skipping firewall setup"
fi

# 6. Build and start
echo "[6/6] Building and starting all services..."
docker compose down 2>/dev/null || true
docker compose build --no-cache
docker compose up -d

# Wait for healthy
echo ""
echo "Waiting for services..."
sleep 15
docker compose ps

echo ""
echo "============================================"
echo "   CryptoForge is LIVE!"
echo "============================================"
echo ""
echo "  Dashboard:   http://${VPS_IP}:8050"
echo "  Grafana:     http://${VPS_IP}:3000"
echo "  Prometheus:  http://${VPS_IP}:9091"
echo ""
echo "  Bot logs:    docker compose logs -f cryptoforge"
echo "  Restart:     docker compose restart cryptoforge"
echo "  Stop all:    docker compose down"
echo "  Start all:   docker compose up -d"
echo "  Update:      git pull && docker compose up -d --build"
echo ""
echo "  Auto-restart: Docker 'restart: always' is enabled."
echo "  The bot will survive VPS reboots automatically."
echo "============================================"
