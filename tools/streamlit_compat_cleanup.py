from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}

REPLACEMENTS = (
    (re.compile(r"\buse_container_width\s*=\s*True\b"), 'width="stretch"'),
    (re.compile(r"\buse_container_width\s*=\s*False\b"), 'width="content"'),
)


def iter_python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def main() -> int:
    changed_files = 0
    replacements = 0

    for path in iter_python_files():
        text = path.read_text(encoding="utf-8")
        updated = text
        local_count = 0
        for pattern, replacement in REPLACEMENTS:
            updated, count = pattern.subn(replacement, updated)
            local_count += count
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed_files += 1
            replacements += local_count
            print(f"UPDATED {path.relative_to(ROOT)} replacements={local_count}")

    unresolved = []
    for path in iter_python_files():
        text = path.read_text(encoding="utf-8")
        if "use_container_width" in text:
            lines = [
                str(i)
                for i, line in enumerate(text.splitlines(), start=1)
                if "use_container_width" in line
            ]
            unresolved.append(f"{path.relative_to(ROOT)}:{','.join(lines[:20])}")

    print(f"STREAMLIT_COMPAT changed_files={changed_files} replacements={replacements}")
    if unresolved:
        print("STREAMLIT_COMPAT unresolved use_container_width occurrences:")
        for item in unresolved:
            print(f"  {item}")
        return 2

    print("STREAMLIT_COMPAT OK remaining_use_container_width=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
