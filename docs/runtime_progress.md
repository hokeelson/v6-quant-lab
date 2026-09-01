# Main-worker progress and startup readiness

This change adds observability and adjusts stall detection. It does not change
entry rules, position sizing, exits, account balances, or broker execution.

## What is reported

- `progress_schema_version = WORKER_PROGRESS_V1`
- Current phase, asset / horizon, phase completed / total counts.
- `last_progress_at` and `progress_events`, advanced only by the main work loop.
- Current-cycle phase durations and a separate completed-cycle timing summary.
- Live checked-pair / processed-bar counters rather than the preceding cycle's
  counters. A phase's total includes skipped unready pairs; `assets_checked`
  counts attempted ready pairs, preserving the previous result semantics.
- `first_cycle_complete`, plus public `ready` / `starting` indicators.

Both dashboards and both public JSON snapshots expose these fields. Heartbeat
updates refresh elapsed-time displays but do **not** refresh work progress.
Timing uses a monotonic clock. Public fields are explicitly allowlisted.

## Watchdog policy

- Existing heartbeat protection remains active (supervisor: 240 seconds).
- New workers: restart after **900 seconds without work progress**, even when
  the heartbeat is fresh. Productive cycles may exceed the old 15-minute limit.
- An independent **3,600-second absolute cycle limit** cannot be renewed by
  progress events.
- Workers without versioned progress retain their prior 900-second cycle limit.
- Missing, previous-cycle, or future-dated progress is invalid.
- After launching a process, the supervisor requires status from that PID and
  allows the existing startup grace period before treating missing status as a
  failure. A previous process's stale status cannot immediately kill a new one.
- The public health exporter and supervisor share the same running-cycle policy.
- A fresh heartbeat during the first cycle is `STARTING`, not `HEALTHY`.
  Existing error / degraded indicators still take priority.

Heartbeat and main-loop progress writes are serialized through one lock so their
atomic temporary-file replacements cannot race. Observer failures do not change
trading-calculation outcomes.

## Verification

Run `python -m pytest -q`. Tests cover timing, productive long cycles, real stalls,
invalid timestamps, absolute limits, PID handoff, initialization readiness,
incremental simulation counts, error paths, concurrent status writes, public
field filtering, and shared dashboard rendering. Existing entry-gate and
simulation-execution safety tests must remain green.

## Deployment / acceptance

Do not redeploy solely because the V10 backup is healthy. First verify a fresh,
consistent backup of `simulation_lab.sqlite3` and the other irreplaceable ledgers
in persistent storage. General storage status `UNKNOWN` is not backup evidence.

After deployment, verify `WORKER_PROGRESS_V1` in a freshly timestamped health
snapshot; phase / unit / counters should advance before the first cycle ends.
The first cycle must report `STARTING` until completion. Inspect the completed
phase timing summary to identify expensive stages before optimizing them.
Confirm retained ledger counts and new entry-gate outcomes separately. Do not
interpret zero events, a green heartbeat, or this change as profitability proof.

GitHub snapshot synchronization is scheduled separately and may lag the direct
Railway dashboard. Always distinguish snapshot time from inspection time.
