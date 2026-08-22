from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    raw = os.getenv("V6_DATA_DIR", "").strip()
    path = Path(raw).expanduser() if raw else Path(".")
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path(filename: str) -> str:
    return str(data_dir() / filename)
