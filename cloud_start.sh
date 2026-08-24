#!/usr/bin/env sh
set -eu

PERSIST_DIR="${V6_DATA_DIR:-/data}"
RUNTIME_DIR="/tmp/v6-data-runtime"

mkdir -p "$PERSIST_DIR" "$RUNTIME_DIR"

# Railway persistent volume has returned SQLite disk I/O errors. Keep active
# SQLite files on container-local storage and persist only checked snapshots.
export V6_PERSISTENT_DATA_DIR="$PERSIST_DIR"
export V6_RUNTIME_DATA_DIR="$RUNTIME_DIR"
export V6_DATA_DIR="$RUNTIME_DIR"
export V6_STORAGE_DEGRADED="1"
export V6_SNAPSHOT_INTERVAL_SECONDS="${V6_SNAPSHOT_INTERVAL_SECONDS:-300}"
export V6_SNAPSHOT_KEEP="${V6_SNAPSHOT_KEEP:-6}"

# Recover the newest healthy copy: a validated persisted snapshot when available,
# otherwise a readable original DB (+ WAL/SHM family) from the Railway volume.
python storage_rescue.py bootstrap

APP_PORT="${PORT:-8501}"
echo "Starting V6 unified Streamlit dashboard on 0.0.0.0:${APP_PORT}"
echo "Virtual simulation only; broker order API remains disabled."
echo "SQLite rescue mode: runtime=${RUNTIME_DIR}; persistent snapshots=${PERSIST_DIR}/v6-snapshots."

python worker_supervisor_v8.py &
SUPERVISOR_PID=$!
python realtime_supervisor.py &
REALTIME_SUPERVISOR_PID=$!
python tca_supervisor.py &
TCA_SUPERVISOR_PID=$!
python storage_rescue.py watch &
STORAGE_PID=$!

cleanup() {
  # Best-effort final consistent snapshot before shutdown. Failure must never
  # block service termination or modify the preserved original DB files.
  python storage_rescue.py snapshot >/dev/null 2>&1 || true
  kill "$SUPERVISOR_PID" 2>/dev/null || true
  kill "$REALTIME_SUPERVISOR_PID" 2>/dev/null || true
  kill "$TCA_SUPERVISOR_PID" 2>/dev/null || true
  kill "$STORAGE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec streamlit run dashboard_v8.py \
  --server.address=0.0.0.0 \
  --server.port="${APP_PORT}" \
  --server.headless=true \
  --browser.gatherUsageStats=false
