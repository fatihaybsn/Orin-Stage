#!/usr/bin/env python3
"""Inspect or remove only stale/incomplete JP6.2.3 Step 3 attempts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orin_stage.base.cleanup import (  # noqa: E402
    inspect_jp623_base_attempts,
    remove_jp623_base_attempts,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.data_root.expanduser().resolve()

    inspection = inspect_jp623_base_attempts(root)
    for entry in inspection.protected:
        print(f"KEEP   {entry.kind:7} {entry.path} ({entry.reason})")
    for entry in inspection.removable:
        action = "REMOVE" if args.apply else "WOULD-REMOVE"
        print(f"{action:12} {entry.kind:7} {entry.path} ({entry.reason})")

    if args.apply:
        removed = remove_jp623_base_attempts(root)
        for entry in removed:
            print(f"REMOVED {entry.kind:7} {entry.path}")
        print(f"cleanup: removed {len(removed)} JP6.2.3 Step-3 attempt(s)")
    else:
        print(f"cleanup dry-run: {len(inspection.removable)} removable attempt(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
