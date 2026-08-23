#!/usr/bin/env sh
set -eu

mkdir -p "${V6_DATA_DIR:-/data}"

APP_PORT="${PORT:-8501}"
echo "Starting V6 V8 Streamlit on 0.0.0.0:${APP_PORT}"

echo "Virtual simulation only; broker order API remains disabled."
python worker_supervisor_v8.py &
SUPERVISOR_PID=$!

cleanup() {
  kill "$SUPERVISOR_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec streamlit run dashboard_v8.py \
  --server.address=0.0.0.0 \
  --server.port="${APP_PORT}" \
  --server.headless=true \
  --browser.gatherUsageStats=false
