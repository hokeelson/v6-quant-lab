#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="/opt/v6-data"

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sudo sh
fi

sudo systemctl enable --now docker
sudo mkdir -p "$DATA_DIR"
sudo chown "$(id -u):$(id -g)" "$DATA_DIR"

cd "$REPO_DIR"

if [ ! -f .env.oracle ]; then
  cp .env.oracle.example .env.oracle
  echo
  echo "Created .env.oracle. Fill ALPACA_API_KEY, ALPACA_API_SECRET and V6_DASHBOARD_PASSWORD, then run this script again."
  exit 2
fi

if grep -Eq '^ALPACA_API_KEY=$|^ALPACA_API_SECRET=$|^V6_DASHBOARD_PASSWORD=$' .env.oracle; then
  echo "ERROR: .env.oracle still has required blank values."
  exit 2
fi

if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow 80/tcp >/dev/null 2>&1 || true
fi

sudo docker compose -f docker-compose.oracle.yml up -d --build

echo
echo "V6 started on Oracle VM."
echo "Open TCP port 80 in the OCI VCN/Security List, then visit http://<PUBLIC_IP>/"
echo "Persistent data: $DATA_DIR"
echo "Broker order API remains disabled."
