#!/usr/bin/env sh
set -eu

mkdir -p "${V6_DATA_DIR:-/data}"

APP_PORT="${PORT:-8501}"
echo "Starting V6 Streamlit on 0.0.0.0:${APP_PORT}"

python live_worker.py &
WORKER_PID=$!

cleanup() {
  kill "$WORKER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec streamlit run dashboard.py \
  --server.address=0.0.0.0 \
  --server.port="${APP_PORT}" \
  --server.headless=true \
  --browser.gatherUsageStats=false
