#!/usr/bin/env sh
set -eu

PERSIST_DIR="${V6_DATA_DIR:-/data}"
RUNTIME_DIR="/tmp/v6-data-runtime"

mkdir -p "$PERSIST_DIR" "$RUNTIME_DIR"

export V6_PERSISTENT_DATA_DIR="$PERSIST_DIR"
export V6_RUNTIME_DATA_DIR="$RUNTIME_DIR"
export V6_DATA_DIR="$RUNTIME_DIR"
export V6_STORAGE_DEGRADED="1"

# Restore critical SQLite state before any worker starts. storage_rescue.py checks
# both the latest snapshot and original /data copy with PRAGMA quick_check and
# chooses the newest healthy source. If bootstrap itself fails, fall back to the
# previous conservative copy behavior so the dashboard can still start.
BOOTSTRAP_OK=0
if python storage_rescue.py bootstrap 2>/dev/null && [ -f "$RUNTIME_DIR/simulation_lab.sqlite3" ]; then
  BOOTSTRAP_OK=1
  echo "SQLite bootstrap: restored newest healthy critical state."
else
  echo "SQLite bootstrap unavailable; falling back to original persistent copies."
  for f in "$PERSIST_DIR"/*.sqlite3 "$PERSIST_DIR"/*.sqlite3-wal "$PERSIST_DIR"/*.sqlite3-shm; do
    if [ -f "$f" ]; then
      cp -p "$f" "$RUNTIME_DIR/$(basename "$f")" 2>/dev/null || true
    fi
  done
fi

APP_PORT="${PORT:-8501}"
echo "Starting V6 unified Streamlit dashboard on 0.0.0.0:${APP_PORT}"
echo "Virtual simulation only; broker order API remains disabled."
echo "SQLite rescue mode: persistent state=${PERSIST_DIR}; runtime DBs=${RUNTIME_DIR}; bootstrap=${BOOTSTRAP_OK}."
echo "SQLite snapshot sidecar: best-effort only; failures never stop the dashboard."

python worker_supervisor_v8.py &
SUPERVISOR_PID=$!
python realtime_supervisor.py &
REALTIME_SUPERVISOR_PID=$!
python tca_supervisor.py &
TCA_SUPERVISOR_PID=$!
python trial_ledger_worker.py &
TRIAL_LEDGER_PID=$!
# Persistence is deliberately a non-critical sidecar. storage_rescue.py catches
# snapshot errors internally; the shell also treats sidecar exit as non-fatal.
python storage_rescue.py watch &
STORAGE_RESCUE_PID=$!
# Export a separate strict-whitelist persistence JSON. It never competes with the
# research snapshot writer; GitHub Actions merges both read-only files later.
python storage_status_exporter.py &
STORAGE_STATUS_PID=$!

cleanup() {
  kill "$SUPERVISOR_PID" 2>/dev/null || true
  kill "$REALTIME_SUPERVISOR_PID" 2>/dev/null || true
  kill "$TCA_SUPERVISOR_PID" 2>/dev/null || true
  kill "$TRIAL_LEDGER_PID" 2>/dev/null || true
  kill "$STORAGE_RESCUE_PID" 2>/dev/null || true
  kill "$STORAGE_STATUS_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec streamlit run dashboard_v8.py \
  --server.address=0.0.0.0 \
  --server.port="${APP_PORT}" \
  --server.headless=true \
  --browser.gatherUsageStats=false
