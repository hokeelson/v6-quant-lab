from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path


def atomic_write_json(path: str | Path, payload: dict, *, retries: int = 8, retry_delay: float = 0.05) -> None:
    """Atomically replace a JSON status file with Windows-friendly retries.

    A unique temp file prevents heartbeat/progress writers or overlapping process
    shutdown/startup from fighting over the same .tmp path. Permission/sharing
    violations from antivirus/indexers/readers are retried briefly.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    data = json.dumps(payload, ensure_ascii=False)
    try:
        tmp.write_text(data, encoding="utf-8")
        attempts = max(1, int(retries))
        for attempt in range(attempts):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(max(0.0, float(retry_delay)) * (attempt + 1))
            except OSError as exc:
                # Windows sharing/permission violations can surface as either
                # PermissionError or generic OSError (winerror 5/32).
                if getattr(exc, "winerror", None) not in (5, 32) or attempt + 1 >= attempts:
                    raise
                time.sleep(max(0.0, float(retry_delay)) * (attempt + 1))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
