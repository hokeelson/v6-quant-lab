from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STOP_FILE = ROOT / ".v6_local_stop"

# Essential Crypto Lite services. Cloud-only storage rescue/export workers are
# intentionally excluded: local SQLite files live directly on the user's disk.
WORKERS = [
    ("main", [sys.executable, "worker_supervisor_v8.py"]),
    ("realtime", [sys.executable, "realtime_supervisor.py"]),
    ("tca", [sys.executable, "tca_supervisor.py"]),
    ("direction", [sys.executable, "direction_shadow_supervisor.py"]),
    ("external_intelligence", [sys.executable, "external_intelligence_worker.py"]),
    ("binance_context", [sys.executable, "binance_market_context_worker.py"]),
    ("runtime_health", [sys.executable, "runtime_health_exporter.py"]),
    ("policy_epoch", [sys.executable, "policy_epoch_exporter.py"]),
]

DASHBOARD = [
    sys.executable,
    "-m",
    "streamlit",
    "run",
    "dashboard_v9.py",
    "--server.address=127.0.0.1",
    "--server.port=8501",
    "--server.headless=false",
    "--browser.gatherUsageStats=false",
]

processes: dict[str, subprocess.Popen] = {}
shutting_down = False


def ensure_local_environment() -> None:
    data_dir = Path(os.environ.get("V6_DATA_DIR", ROOT / "data_crypto_lite")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    os.environ["V6_SINGLE_CRYPTO_ACCOUNT"] = "1"
    os.environ["V6_ENABLE_CRYPTO_V2_SHADOW"] = "0"
    os.environ["V6_ENABLE_TRIAL_LEDGER"] = "0"
    os.environ["V6_LOCAL_MODE"] = "1"
    os.environ["V6_DATA_DIR"] = str(data_dir)
    os.environ["V6_RUNTIME_DATA_DIR"] = str(data_dir)
    os.environ["V6_PERSISTENT_DATA_DIR"] = str(data_dir)
    os.environ["V6_STORAGE_DEGRADED"] = "0"

    # Never allow this launcher to enable real broker execution.
    os.environ["V6_ALLOW_PAPER_ORDERS"] = "false"


def spawn(name: str, command: list[str]) -> subprocess.Popen:
    print(f"[V6] starting {name}: {' '.join(command)}", flush=True)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=os.environ.copy(),
        creationflags=flags,
    )


def stop_all() -> None:
    global shutting_down
    if shutting_down:
        return
    shutting_down = True
    print("\n[V6] stopping local Crypto Lite stack...", flush=True)

    # Dashboard first, then background workers.
    ordered = ["dashboard"] + [name for name, _ in reversed(WORKERS)]
    for name in ordered:
        proc = processes.get(name)
        if proc is None or proc.poll() is not None:
            continue
        try:
            proc.terminate()
        except Exception:
            pass

    deadline = time.time() + 10
    for proc in processes.values():
        if proc.poll() is not None:
            continue
        remaining = max(0.0, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
        except Exception:
            pass

    for proc in processes.values():
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    try:
        STOP_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    print("[V6] stopped. Local data remains on disk.", flush=True)


def handle_signal(_signum, _frame) -> None:
    stop_all()
    raise SystemExit(0)


def main() -> int:
    ensure_local_environment()
    STOP_FILE.unlink(missing_ok=True)

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    print(f"[V6] local data directory: {os.environ['V6_DATA_DIR']}", flush=True)
    print("[V6] simulation only; broker order API remains disabled.", flush=True)

    try:
        for name, command in WORKERS:
            processes[name] = spawn(name, command)
            time.sleep(0.35)

        processes["dashboard"] = spawn("dashboard", DASHBOARD)

        while True:
            if STOP_FILE.exists():
                print("[V6] stop request received.", flush=True)
                break

            # If an essential child exits, fail closed instead of silently
            # continuing with only part of the trading stack.
            failed = [
                (name, proc.returncode)
                for name, proc in processes.items()
                if proc.poll() is not None
            ]
            if failed:
                print(f"[V6] child process exited: {failed}", flush=True)
                return 1

            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
