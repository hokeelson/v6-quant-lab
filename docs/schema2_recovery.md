# PR23 schema-2-compatible code recovery

## Decision and scope

Do not downgrade `PRAGMA user_version`, drop linkage columns, redeploy the bare
PR22 image, or restore a pre-PR23 ledger over trades written after deployment.
The recovery candidate is **PR22 runtime + the PR23 schema-2 database adapter**.
It withdraws PR23 observation/UI/timing changes while retaining the current
ledger format and atomic entry/exit linkage. This is a new compatibility build,
not an exact binary rollback. Forward repair remains preferable when the fault
is in the retained adapter or when database integrity is in doubt.

Pinned baseline: `8dd8244bd86e656b736abf4944efa0840bea7c97`.
Recovery review branch: `recovery/pr22-schema2-20260903` (resolve and record its
immutable commit before use; never deploy an unreviewed moving branch).
Retained file: `src/simulation_db.py`, SHA256
`978dc87ce5499b452ff984f30c86ccf348b9363710b7da636eed5b2cf362a5e0`.
Other changes on that branch are this runbook, recovery regression tests, and a
main-only push filter for snapshot sync. No strategy/risk settings are relaxed.

## Reproducible compatibility check

The `schema2-recovery` CI job checks out the exact baseline above and overlays
only the candidate DB adapter plus `tests/test_schema2_recovery.py`. It runs the
baseline's full suite as well as recovery tests, independently of the PR23 suite.
Record both successful job URLs and the tested PR head. If the adapter changes,
rebuild/retest the recovery branch and update its digest before release.

Tests use disposable SQLite files, never production. Coverage includes all-table
logical equality and schema version on reopen, cash/position/trade retention,
stock/crypto/Taiwan model and protective closes with entry linkage, stock/crypto
margin closes, Taiwan's cash-only no-op, blocked orders remaining unfilled,
current WAL-aware backup including post-deployment writes, transaction rollback
on a mid-migration failure, retry with legacy NULL links, and future-schema
rejection. These do not prove live-platform recovery or uninterrupted operation.

## If deployment fails

1. First determine whether the old container is still healthy. A build failure
   before replacement does not itself require changing data. Do not issue stale
   recovery/resume commands from another container or an expired lease.
2. If schema 2 was opened/written, preserve the **latest** eight databases. Use
   the reviewed maintenance helper only with explicit operator authorization,
   all other writers/dashboard sessions excluded, fresh process identities,
   valid automatic-resume lease and sufficient disk headroom. Require a new
   READY_FOR_DEPLOY receipt; yesterday's receipt and rolling backup timestamps
   are not a frozen multi-database recovery point.
3. Check SQLite integrity, schema version and all-table counts against that
   fresh receipt, including positions/trades and linked order IDs. Retain an
   independently verified copy of the latest ledger before any replacement.
   Use SQLite backup for a WAL database, not a raw copy of its main file. Do not
   delete independent backups or compact a live ledger to make room. If space,
   integrity, writers or lease state cannot be verified, STOP and escalate.
4. Review the recovery commit's exact diff against the pinned baseline and match
   its adapter digest to the successful compatibility job. Prepare deployment
   as a separately authorized forward commit carrying that recovery tree; do
   not force-push main, use Railway's bare-old-image rollback, or separately
   merge the recovery branch after PR23 (that would not withdraw its features).
5. Deploy once within the validated maintenance window. Bootstrap must use the
   current recovery snapshot, not the original pre-upgrade copy. If the lease
   expires, writers resume, or any identity changes before handoff, abandon
   that readiness receipt and reassess. Do not disable/extend the guardian.
6. Verify actual running commit, fresh heartbeat and a completed cycle, risk and
   data quality, nine retained accounts, historical and post-PR23 trades/cash/
   links, seven general backups plus V10 backup, simulation-only mode and zero
   broker order calls. Original receipt equality applies only while frozen;
   later legitimate trading can change counts and balances. Missing evidence
   is not success. Do not force trades to manufacture acceptance samples.

Recovery removes PR23's public execution-audit and expanded timing evidence.
The retained DB adapter still records entry IDs, but old diagnostics may lack
order/decision IDs; report the audit gap rather than claiming full observability.
Never restore old cash/trade totals just to match a previous report.

## Remaining production gate

No script here pauses, restarts, deploys, modifies schema or restores production.
A tested compatibility build is only one release prerequisite. A fresh,
authorized, consistent eight-database backup and valid receipt are still required
immediately before deployment. Platform crashes, unaccounted writers, expired
maintenance windows and a faulty DB adapter require separate investigation; no
zero-loss guarantee or profitability claim is made.
