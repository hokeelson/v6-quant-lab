from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

RUNTIME_DIR = Path(os.getenv("V6_RUNTIME_DATA_DIR", "/tmp/v6-data-runtime"))
PERSIST_DIR = Path(os.getenv("V6_PERSISTENT_DATA_DIR", "/data"))
MARKER = PERSIST_DIR / ".crypto_lite_clean_baseline_v1.json"
REPORT = RUNTIME_DIR / "crypto_lite_cleanup.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path):
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def table_exists(con, name: str) -> bool:
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def columns(con, name: str) -> set[str]:
    if not table_exists(con, name):
        return set()
    return {str(r[1]) for r in con.execute(f"PRAGMA table_info({name})").fetchall()}


def delete_count(con, sql: str, args=()) -> int:
    cur = con.execute(sql, args)
    return max(0, int(cur.rowcount or 0))


def cleanup_simulation(path: Path, baseline_at: str) -> dict:
    if not path.exists():
        return {"missing": True}
    out = {}
    with connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """INSERT OR IGNORE INTO accounts(account_id,market,horizon,initial_equity,cash,status,created_at)
               VALUES('crypto','crypto','all',100000,100000,'ACTIVE',?)""",
            (now_iso(),),
        )
        con.execute("UPDATE accounts SET status='ACTIVE' WHERE account_id='crypto'")

        for table in ("orders","positions","trades","equity_history","diagnostics","marks"):
            if table_exists(con, table) and "account_id" in columns(con, table):
                out[table] = delete_count(con, f"DELETE FROM {table} WHERE account_id<>'crypto'")

        if table_exists(con, "accounts"):
            out["accounts"] = delete_count(con, "DELETE FROM accounts WHERE account_id<>'crypto'")

        if table_exists(con, "assets"):
            out["assets_noncrypto"] = delete_count(con, "DELETE FROM assets WHERE market<>'crypto'")
        if table_exists(con, "models"):
            out["models_noncrypto"] = delete_count(con, "DELETE FROM models WHERE market<>'crypto'")

        if table_exists(con, "decisions"):
            out["decisions_noncrypto"] = delete_count(con, "DELETE FROM decisions WHERE market<>'crypto'")
            out["decisions_prebaseline_pseudo"] = delete_count(
                con,
                """DELETE FROM decisions
                   WHERE account_id<>'crypto' AND created_at<?
                     AND decision_id NOT IN (
                       SELECT decision_id FROM orders
                       WHERE account_id='crypto' AND status='PENDING' AND decision_id IS NOT NULL
                     )""",
                (baseline_at,),
            )

        if table_exists(con, "engine_state"):
            out["engine_state_legacy"] = delete_count(
                con,
                """DELETE FROM engine_state
                   WHERE account_id NOT IN ('crypto_short','crypto_medium','crypto_long')""",
            )
        con.commit()
    return out


def cleanup_forward(path: Path, first_run: bool) -> dict:
    if not path.exists():
        return {"missing": True}
    out = {}
    with connect(path) as con:
        con.execute("PRAGMA foreign_keys=ON")
        if table_exists(con, "candidates") and "market" in columns(con, "candidates"):
            if first_run:
                out["candidates_reset"] = delete_count(con, "DELETE FROM candidates")
            else:
                out["candidates_noncrypto"] = delete_count(con, "DELETE FROM candidates WHERE market<>'crypto'")
        con.commit()
    return out


def cleanup_market_cache(path: Path) -> dict:
    if not path.exists():
        return {"missing": True}
    out = {}
    with connect(path) as con:
        for table in ("bars", "fetch_state"):
            if table_exists(con, table) and "market" in columns(con, table):
                out[table] = delete_count(con, f"DELETE FROM {table} WHERE market<>'crypto'")
        con.commit()
    return out


def cleanup_direction(path: Path, baseline_at: str) -> dict:
    if not path.exists():
        return {"missing": True}
    out = {}
    with connect(path) as con:
        if table_exists(con, "direction_predictions"):
            out["noncrypto"] = delete_count(con, "DELETE FROM direction_predictions WHERE market<>'crypto'")
            out["prebaseline"] = delete_count(
                con, "DELETE FROM direction_predictions WHERE created_at<?", (baseline_at,)
            )
        con.commit()
    return out


def cleanup_governance(path: Path, first_run: bool) -> dict:
    if not path.exists():
        return {"missing": True}
    out = {}
    with connect(path) as con:
        if first_run:
            for table in ("arena_snapshots", "governance_events", "arenas", "research_state"):
                if table_exists(con, table):
                    out[table + "_reset"] = delete_count(con, f"DELETE FROM {table}")
        else:
            if table_exists(con, "arenas"):
                ids = [str(r[0]) for r in con.execute("SELECT arena_id FROM arenas WHERE market<>'crypto'").fetchall()]
                if ids and table_exists(con, "arena_snapshots"):
                    marks = ",".join("?" for _ in ids)
                    out["arena_snapshots_noncrypto"] = delete_count(
                        con, f"DELETE FROM arena_snapshots WHERE arena_id IN ({marks})", ids
                    )
                out["arenas_noncrypto"] = delete_count(con, "DELETE FROM arenas WHERE market<>'crypto'")
            for table in ("research_state", "governance_events"):
                if table_exists(con, table) and "market" in columns(con, table):
                    out[table] = delete_count(con, f"DELETE FROM {table} WHERE market<>'crypto'")
        con.commit()
    return out


def cleanup_data_quality(path: Path, first_run: bool) -> dict:
    if not path.exists():
        return {"missing": True}
    out = {}
    with connect(path) as con:
        for table in ("health_latest", "health_events"):
            if not table_exists(con, table):
                continue
            if first_run:
                out[table + "_reset"] = delete_count(con, f"DELETE FROM {table}")
            elif "market" in columns(con, table):
                out[table + "_noncrypto"] = delete_count(con, f"DELETE FROM {table} WHERE market<>'crypto'")
        con.commit()
    return out


def cleanup_realtime(path: Path, first_run: bool) -> dict:
    if not path.exists():
        return {"missing": True}
    out = {}
    with connect(path) as con:
        for table in ("watchlist", "quotes", "signals", "ticks"):
            if not table_exists(con, table):
                continue
            if first_run:
                out[table] = delete_count(con, f"DELETE FROM {table}")
            elif "market" in columns(con, table):
                out[table] = delete_count(con, f"DELETE FROM {table} WHERE market<>'crypto'")
        con.commit()
    return out


def remove_obsolete_files() -> list[str]:
    removed = []
    # Realtime execution and market cache are rebuildable runtime caches in
    # Crypto Lite. Never keep old persistent copies on the small Railway volume.
    names = (
        "trial_ledger.sqlite3",
        "crypto_v2_shadow.sqlite3",
        "realtime_execution.sqlite3",
        "market_cache.sqlite3",
    )
    roots = [
        RUNTIME_DIR,
        PERSIST_DIR,
        PERSIST_DIR / "v6-snapshots" / "current",
    ]
    for root in roots:
        for name in names:
            for suffix in ("", "-wal", "-shm", ".new"):
                p = root / (name + suffix)
                try:
                    if p.exists():
                        p.unlink()
                        removed.append(str(p))
                except Exception:
                    pass
    for prefix in ("pr20-backup-", "pr20-restore-safety-", "pr20-realtime-safe-", "entry-gate-backup-"):
        try:
            for p in PERSIST_DIR.glob(prefix + "*"):
                if p.is_dir():
                    import shutil
                    shutil.rmtree(p, ignore_errors=True)
                    removed.append(str(p))
        except Exception:
            pass

    # Remove stale root copies only when a current healthy snapshot exists.
    # These August-era files were repeatedly mistaken for live runtime state.
    current_dir = PERSIST_DIR / "v6-snapshots" / "current"
    for name in ("simulation_lab.sqlite3", "model_governance.sqlite3", "data_quality.sqlite3"):
        if not (current_dir / name).exists():
            continue
        for suffix in ("", "-wal", "-shm"):
            p = PERSIST_DIR / (name + suffix)
            try:
                if p.exists():
                    p.unlink()
                    removed.append(str(p))
            except Exception:
                pass

    for name in (
        "realtime_status.json", "realtime_status.json.tmp",
        "worker_status.json", "tca_status.json", "tca_status.json.tmp",
        "dynamic_universe.json", "pretrade_risk_snapshot.json",
        "professional_risk_snapshot.json",
        "pr22-maintenance-5854c90t-receipt.json",
        "pr22-maintenance-3982zwwt-receipt.json",
    ):
        p = PERSIST_DIR / name
        try:
            if p.exists():
                p.unlink()
                removed.append(str(p))
        except Exception:
            pass

    # Clean orphaned temp sidecars left by retired Shadow workers.
    try:
        if current_dir.exists():
            for p in current_dir.iterdir():
                if not p.is_file():
                    continue
                if p.name.startswith(".crypto_v2_shadow.") or ".tmp-" in p.name:
                    try:
                        p.unlink()
                        removed.append(str(p))
                    except Exception:
                        pass
    except Exception:
        pass

    # ForwardDB is no longer a production input in Crypto Lite. Keep the runtime
    # file if a compatibility class recreates it, but remove old persistent copies
    # so bootstrap can never revive the legacy candidate pool.
    for root in (PERSIST_DIR, PERSIST_DIR / "v6-snapshots" / "current"):
        for suffix in ("", "-wal", "-shm"):
            p = root / ("forward_validation.sqlite3" + suffix)
            try:
                if p.exists():
                    p.unlink()
                    removed.append(str(p))
            except Exception:
                pass

    for p in (
        Path("static") / "crypto_v2_shadow_snapshot.json",
        RUNTIME_DIR / "crypto_v2_shadow_worker_status.json",
    ):
        try:
            if p.exists():
                p.unlink()
                removed.append(str(p))
        except Exception:
            pass
    return removed


def main():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PERSIST_DIR.mkdir(parents=True, exist_ok=True)

    first_run = not MARKER.exists()
    if first_run:
        baseline_at = now_iso()
    else:
        try:
            baseline_at = str(json.loads(MARKER.read_text(encoding="utf-8")).get("baseline_at") or now_iso())
        except Exception:
            baseline_at = now_iso()

    report = {
        "generated_at": now_iso(),
        "baseline_at": baseline_at,
        "first_run": first_run,
        "simulation": cleanup_simulation(RUNTIME_DIR / "simulation_lab.sqlite3", baseline_at),
        "forward": cleanup_forward(RUNTIME_DIR / "forward_validation.sqlite3", first_run),
        "market_cache": cleanup_market_cache(RUNTIME_DIR / "market_cache.sqlite3"),
        "direction": cleanup_direction(RUNTIME_DIR / "direction_forward.sqlite3", baseline_at),
        "governance": cleanup_governance(RUNTIME_DIR / "model_governance.sqlite3", first_run),
        "data_quality": cleanup_data_quality(RUNTIME_DIR / "data_quality.sqlite3", first_run),
        "realtime": cleanup_realtime(RUNTIME_DIR / "realtime_execution.sqlite3", first_run),
        "removed_files": remove_obsolete_files(),
    }

    if first_run:
        tmp = MARKER.with_suffix(".tmp")
        tmp.write_text(json.dumps({"baseline_at": baseline_at}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(MARKER)

    tmp_report = REPORT.with_suffix(".json.tmp")
    tmp_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_report.replace(REPORT)
    print("CRYPTO_LITE_CLEANUP", json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
