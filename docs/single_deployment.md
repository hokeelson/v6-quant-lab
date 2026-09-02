# Single deployment of worker and storage fixes

Integrates PR #20 and PR #21 without two main-branch deployments. Neither
strategy decisions nor live/short broker execution is enabled by this change.

## Before readiness

The operator must keep the dashboard tab closed (PID 1 and SSH remain running).
Run `ops/prepare_deploy.py --backup-module <verified storage_rescue.py>` only
after the exact integration commit passes CI. Download both scripts by immutable
commit SHA and verify their SHA256 digests before execution. The helper is
never invoked by cloud_start.sh and does not deploy by itself.

It identifies the 16 expected worker/supervisor/exporter processes, checks their
start identities, establishes a detached 15-minute recovery guardian, and pauses
them. In one invocation it snapshots all eight critical databases using the
bounded read-only backup implementation, verifies table counts and file hashes,
and writes a small durable receipt. It does not create another retained 1.4GB
raw copy. Existing independent compressed backups are not touched. A nonempty
restore WAL, changed process, timeout, failed copy or validation aborts readiness
and sends recovery signals. Any recovery errors must be investigated; signal
delivery does not prove worker health.

## Readiness and merge gate

Require READY_FOR_DEPLOY, all eight DBs verified, a current receipt timestamp,
and sufficient time remaining on the printed recovery lease. If the lease
expired or work resumed, the receipt is historical and cannot authorize a
later deployment as a frozen final snapshot. Do not extend a lease by editing
timestamps or disabling its guardian.

Merge the tested integration PR once with expected-head protection. Do not
separately merge PR #20/#21 or apply the unrelated staged Railway PORT edit.
Do not clear account/position/order/trade history or restore older copies to
make row counts match.

## Acceptance

- Confirm Railway's actual deployment result/version; a successful GitHub
  merge or CI run alone is not deployment success.
- Confirm WORKER_PROGRESS_V1, a current heartbeat and advancing phase/counters;
  then a completed cycle and healthy risk/data-quality layers.
- Compare the readiness receipt against restored ledgers before treating normal
  new writes as discrepancies; retained accounts and historical trades must
  not be reset. V10 evidence and backup must remain healthy.
- Require fresh storage success including realtime_execution.sqlite3 and no
  failures, with staging headroom preserved.
- Check ENTRY_GATE_V1 samples and specifically entry_allowed=false with
  filled=true. Zero samples / WAITING_FOR_EVENTS is not acceptance.
- Keep simulation_only=true and broker_order_api_calls=0.

No zero-loss guarantee is made for other dashboard sessions writing during
maintenance, expiry followed by an unusually slow deployment, platform-level
crashes, or uninterruptible I/O. If a gate fails, stop and report the evidence;
do not silently reset state or force a protected merge. Code rollback and data
rollback are separate decisions, and newer ledgers must be preserved.
