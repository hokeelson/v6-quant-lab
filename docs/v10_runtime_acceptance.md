# V10 runtime repair acceptance

This repair changes runtime wiring and observability, not signal weights or execution authority.

## Root cause
The direction worker opened relative simulation/cache paths under the application cwd while
the main worker used V6_DATA_DIR. A fresh empty simulation DB can have zero assets without an
exception. The old health report did not cover this worker. The direction ledger was also
absent from the rescue snapshot set.

## Repair
- Resolve both input databases and the direction ledger through db_path.
- Refuse to create missing input databases; retry with an explicit ERROR status.
- Read shared cached closed bars only; no additional market-data or broker calls.
- Publish a 15-second heartbeat, cycle timestamps, candidate/registration counts, model/cache
  skips and errors. No candidates or an empty ledger is DEGRADED, never ONLINE.
- Supervise the process for exits, stale heartbeats and stalled cycles.
- Add direction_forward.sqlite3 to mandatory rescue backups, including old env overrides.
  Persist it first, publish individual backup success/failure, and restore it on bootstrap.
- Include direction worker and backup freshness in overall runtime health.

## Automated acceptance
tests/test_direction_runtime.py exercises real temporary SQLite DBs with V6_DATA_DIR different
from cwd, an empty cwd decoy DB, registration idempotence, synthetic closed-bar evaluation,
missing inputs/models/cache, error status, supervision and snapshot/restore identity.
Synthetic test outcomes are never written to production.

## Deployment acceptance
Do not infer success from CI alone. In a post-deployment runtime_health.json snapshot:
- direction_v10.input_path_mode is SHARED_V6_DATA_DIR
- candidates > 0 and pending + evaluated > 0
- heartbeat and completed cycle are fresh; errors are visible
- backup_healthy is true with a fresh backup_at
- broker_order_api_calls and direction market_data_api_calls remain zero

Cross-check direction_shadow.pending in research_snapshot.json. Zero evaluated outcomes on
the first repaired cycle is expected; zero total registrations is not a pass.
The health snapshot copied into GitHub is timestamped backup evidence, not a continuous monitor.

## Independent backup acceptance
V10 backups run on their own 60-second loop, with a 20-second SQLite-copy deadline.
They do not wait for large legacy snapshots. The backup status reports the pending/evaluated
counts read from the persisted snapshot itself. A recent but empty snapshot does not count
as a healthy backup of a nonempty ledger.
