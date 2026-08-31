#!/usr/bin/env sh
set -eu

PERSIST_DIR="${V6_DATA_DIR:-/data}"
RUNTIME_DIR="/tmp/v6-data-runtime"

mkdir -p "$PERSIST_DIR" "$RUNTIME_DIR"

export V6_PERSISTENT_DATA_DIR="$PERSIST_DIR"
export V6_RUNTIME_DATA_DIR="$RUNTIME_DIR"
export V6_DATA_DIR="$RUNTIME_DIR"
export V6_STORAGE_DEGRADED="1"
export V6_SNAPSHOT_DBS="${V6_SNAPSHOT_DBS:-crypto_v2_shadow.sqlite3,data_quality.sqlite3,forward_validation.sqlite3,model_governance.sqlite3,realtime_execution.sqlite3,simulation_lab.sqlite3,trial_ledger.sqlite3}"

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
echo "Starting V6 V9 decision dashboard on 0.0.0.0:${APP_PORT}"
echo "Virtual simulation only; broker order API remains disabled."
echo "Crypto V2 Shadow = isolated ledger, shared cache only, no extra market-data API calls, supervised auto-restart enabled."
echo "SQLite rescue mode: persistent state=${PERSIST_DIR}; runtime DBs=${RUNTIME_DIR}; bootstrap=${BOOTSTRAP_OK}."
echo "SQLite snapshot sidecar: best-effort only; failures never stop the dashboard."
echo "Runtime health: Railway publishes static/runtime_health.json every 5 seconds; GitHub snapshots are backup only."
echo "Policy epoch report: static/policy_epoch_performance.json separates new-policy Shadow evidence from legacy PnL."
echo "External intelligence: public macro/news/market context refreshes every 6 hours and can only reduce virtual risk."
echo "Direction Shadow: LONG/SHORT/NO_TRADE research snapshot refreshes every 15 minutes; short execution remains disabled."

python worker_supervisor_v8.py &
SUPERVISOR_PID=$!
python realtime_supervisor.py &
REALTIME_SUPERVISOR_PID=$!
python tca_supervisor.py &
TCA_SUPERVISOR_PID=$!
python trial_ledger_worker.py &
TRIAL_LEDGER_PID=$!
python crypto_v2_shadow_supervisor.py &
CRYPTO_V2_SUPERVISOR_PID=$!
python storage_rescue.py watch &
STORAGE_RESCUE_PID=$!
python storage_status_exporter.py &
STORAGE_STATUS_PID=$!
python runtime_health_exporter.py &
RUNTIME_HEALTH_PID=$!
python policy_epoch_exporter.py &
POLICY_EPOCH_PID=$!
python external_intelligence_worker.py &
EXTERNAL_INTELLIGENCE_PID=$!
python direction_shadow_worker.py &
DIRECTION_SHADOW_PID=$!

cleanup() {
  kill "$SUPERVISOR_PID" 2>/dev/null || true
  kill "$REALTIME_SUPERVISOR_PID" 2>/dev/null || true
  kill "$TCA_SUPERVISOR_PID" 2>/dev/null || true
  kill "$TRIAL_LEDGER_PID" 2>/dev/null || true
  kill "$CRYPTO_V2_SUPERVISOR_PID" 2>/dev/null || true
  kill "$STORAGE_RESCUE_PID" 2>/dev/null || true
  kill "$STORAGE_STATUS_PID" 2>/dev/null || true
  kill "$RUNTIME_HEALTH_PID" 2>/dev/null || true
  kill "$POLICY_EPOCH_PID" 2>/dev/null || true
  kill "$EXTERNAL_INTELLIGENCE_PID" 2>/dev/null || true
  kill "$DIRECTION_SHADOW_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec streamlit run dashboard_v9.py \
  --server.address=0.0.0.0 \
  --server.port="${APP_PORT}" \
  --server.headless=true \
  --browser.gatherUsageStats=false
