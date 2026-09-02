# Bounded, observable SQLite backups

This storage-only change is independent of PR #20 (worker progress). It does
not change trading rules, account balances, fills, exits, or broker access.

## Observed incident and limits of the diagnosis

The production error at 2026-09-02T15:43:30Z was ENOSPC while copying the
realtime database into the persistent `.new` file. A separately retained raw
backup consumed staging headroom; it was subsequently replaced with a
verified, losslessly compressed backup. A status file still reporting that
timestamp is historical evidence, not proof of a new ENOSPC error.

The old backup implementation did not pin a source read transaction, had no
default deadline for non-direction databases, and only published the aggregate
result at the end of a round. These are confirmed code weaknesses; no captured
production stack trace establishes the current program counter.

## Changes

- Read-only source connections with a pinned committed SQLite read snapshot.
  Concurrent WAL commits cannot continually move the backup's source view.
- Default 90-second backup/verification budget; direction retains 20 seconds.
  SQLite busy waits are bounded to five seconds. These limits are cooperative,
  not a guarantee against an uninterruptible OS I/O operation.
- Unique local staging files and verification before replacement. Missing
  sources are errors, never silently created empty databases.
- Space preflight for a full new persistent image plus 64 MiB reserve. This
  does not reserve disk against other writers; copy failures remain handled.
- Chunked persistent copy, a cooperative 90-second copy budget, file fsync,
  and atomic replacement. Partial copy failures keep the previous current
  image and remove only the failed `.new` file.
- Per-database PREPARE / SQLITE_BACKUP / VERIFY / PERSIST_COPY progress, with
  page or byte counters. The existing last_snapshot_* fields continue to
  describe the last completed round. IDLE marks a completed round.
- Missing configured critical databases make the round fail instead of
  disappearing from the success report.

Current progress fields are whitelisted in static/storage_persistence.json.
They do not imply backup success. Accept a round only with a new
last_snapshot_at, the expected successful databases, and an empty failure list.
Direction has its separate backup result in normal watch mode.

## Deployment hold

Do not merge merely because tests pass. Merging main redeploys Railway.
First establish fresh restorable state and sufficient staging headroom.
This patch does not solve coordinated shutdown/final-snapshot recovery-point
objectives, arbitrary external writes to restore files, or backup retention
growth. It does not guarantee profitability.
