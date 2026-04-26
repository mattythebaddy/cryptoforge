#!/bin/bash
# CryptoForge VPS Deploy Script
# Usage: ssh into your VPS, clone the repo, then run this script.
#
# Recommended VPS: Hetzner CX22 ($4.50/mo) or DigitalOcean $6/mo droplet
#   - 2 vCPU, 4GB RAM, Ubuntu 24.04
#
# Steps:
#   1. Create a VPS with Ubuntu 24.04
#   2. SSH in: ssh root@your-vps-ip
#   3. Clone:  git clone <your-repo-url> /opt/cryptoforge
#   4. cd /opt/cryptoforge
#   5. cp .env.example .env && nano .env   (fill in your secrets)
#   6. bash deploy.sh

set -euo pipefail

echo "=== CryptoForge Deploy ==="

# 1. Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "[1/4] Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
else
    echo "[1/4] Docker already installed"
fi

# 2. Install Docker Compose plugin if not present
if ! docker compose version &> /dev/null; then
    echo "[2/4] Installing Docker Compose plugin..."
    apt-get update && apt-get install -y docker-compose-plugin
else
    echo "[2/4] Docker Compose already installed"
fi

# 3. Check .env
if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy .env.example to .env and fill in your secrets:"
    echo "  cp .env.example .env && nano .env"
    exit 1
fi
echo "[3/4] .env found"

# 4. Build and start
echo "[4/4] Building and starting all services..."
docker compose down 2>/dev/null || true
docker compose build --no-cache
docker compose up -d

echo ""
echo "=== Deployed! ==="
echo "Bot:        docker compose logs -f cryptoforge"
echo "Grafana:    http://$(hostname -I | awk '{print $1}'):3000"
echo "Prometheus: http://$(hostname -I | awk '{print $1}'):9091"
echo ""
echo "Useful commands:"
echo "  docker compose logs -f cryptoforge   # live bot logs"
echo "  docker compose restart cryptoforge   # restart bot"
echo "  docker compose down                  # stop everything"
echo "  docker compose up -d                 # start everything"
