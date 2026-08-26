# V6 Quant Lab — Current Production Architecture

This file describes the currently deployed production path. Older Stage 4–7 quickstart notes are historical references only.

## Production entrypoints

- Railway: `railway.toml` → `/app/cloud_start.sh`
- Dashboard: `dashboard_v8.py`
- Core background worker: `worker_supervisor_v8.py` → `live_worker_v8.py`
- Additional sidecars: realtime supervisor, TCA supervisor, trial ledger worker, storage rescue, storage status exporter
- Core orchestration: `src/auto_orchestrator_v8.py`
- Simulation execution: `src/simulation_engine.py`
- SQLite state: `src/simulation_db.py`
- Taiwan market adapter: `src/twstock_support.py`

## Markets and accounts

Production supports Crypto, US stocks and Taiwan stocks. Each market has short / medium / long virtual sleeves, for nine virtual accounts total. Broker order APIs remain disabled; this is simulation/research infrastructure.

## Execution safety invariants

- A decision formed after bar close can execute no earlier than the next eligible bar open.
- Stale pending BUY orders are cancelled when a position already exists.
- Stale pending SELL orders are cancelled when no position exists.
- Zero-size / non-fillable pending entries are cancelled instead of remaining pending forever.
- BUY fills update order + cash + position in one SQLite transaction.
- SELL fills update order + cash + realized trade + position deletion in one SQLite transaction.
- Protective exits and margin liquidations update cash + realized trade + position deletion atomically.
- Gap-down stop exits use the opening price rather than assuming execution at the stale stop trigger.

## Persistence

Runtime SQLite databases operate from the runtime data directory and critical state is rescued to persistent storage. The default snapshot interval is 60 seconds. `storage_rescue.py` validates restore candidates with SQLite `PRAGMA quick_check` before bootstrap.

## Validation and CI

`.github/workflows/ci.yml` runs the pytest suite on every push to `main` and on pull requests. Execution regression tests include stale-order cancellation, atomic/idempotent sell behavior and gap-stop handling.

## Long-term workflows

Only workflows that serve an ongoing purpose should remain under `.github/workflows/`:

- `ci.yml`
- `ingest-v6.yml`
- `sync-research-snapshot.yml`

One-shot repair workflows and patch scripts should be removed after their changes are merged.

## Historical notes

The former `STAGE4_QUICKSTART.txt` through `STAGE7_QUICKSTART.txt` files are stored under `docs/history/`. They document how the system evolved and should not be treated as the current production source of truth.
