#!/bin/bash
# CryptoForge VPS deployment script
set -euo pipefail

echo "=== CryptoForge Deployment ==="

# Check docker
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Install Docker first."
    exit 1
fi

# Check .env
if [ ! -f .env ]; then
    echo "No .env file found. Copy .env.example to .env and fill in your values."
    exit 1
fi

# Build and start
echo "Building containers..."
docker compose build --no-cache

echo "Starting services..."
docker compose up -d

echo "Waiting for services to be healthy..."
sleep 10

# Check health
docker compose ps

echo ""
echo "=== Deployment Complete ==="
echo "Grafana:    http://localhost:3000"
echo "Prometheus: http://localhost:9091"
echo "Metrics:    http://localhost:9090"
echo ""
echo "Logs: docker compose logs -f cryptoforge"
