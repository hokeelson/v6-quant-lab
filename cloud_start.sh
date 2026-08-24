#!/usr/bin/env sh
set -eu

PERSIST_DIR="${V6_DATA_DIR:-/data}"
RUNTIME_DIR="/tmp/v6-data-runtime"

mkdir -p "$PERSIST_DIR" "$RUNTIME_DIR"

# Railway persistent volume is currently returning SQLite disk I/O errors.
# Preserve every original DB in /data and run from a container-local working copy.
# Copy main SQLite files together with any WAL/SHM sidecars so SQLite can recover
# the latest committed state locally. Never delete or modify the originals here.
for f in "$PERSIST_DIR"/*.sqlite3 "$PERSIST_DIR"/*.sqlite3-wal "$PERSIST_DIR"/*.sqlite3-shm; do
  if [ -f "$f" ]; then
    cp -p "$f" "$RUNTIME_DIR/$(basename "$f")" 2>/dev/null || true
  fi
done

export V6_PERSISTENT_DATA_DIR="$PERSIST_DIR"
export V6_DATA_DIR="$RUNTIME_DIR"
export V6_STORAGE_DEGRADED="1"

APP_PORT="${PORT:-8501}"
echo "Starting V6 unified Streamlit dashboard on 0.0.0.0:${APP_PORT}"
echo "Virtual simulation only; broker order API remains disabled."
echo "SQLite rescue mode: originals preserved in ${PERSIST_DIR}; runtime DBs use ${RUNTIME_DIR}."

python worker_supervisor_v8.py &
SUPERVISOR_PID=$!
python realtime_supervisor.py &
REALTIME_SUPERVISOR_PID=$!
python tca_supervisor.py &
TCA_SUPERVISOR_PID=$!

cleanup() {
  kill "$SUPERVISOR_PID" 2>/dev/null || true
  kill "$REALTIME_SUPERVISOR_PID" 2>/dev/null || true
  kill "$TCA_SUPERVISOR_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec streamlit run dashboard_v8.py \
  --server.address=0.0.0.0 \
  --server.port="${APP_PORT}" \
  --server.headless=true \
  --browser.gatherUsageStats=false
