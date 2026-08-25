from __future__ import annotations

import argparse
from pathlib import Path

from orin_stage.storage import StorageManager


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description="Show Orin Stage MVP storage usage")
    parser.add_argument("data_root", type=Path)
    args = parser.parse_args()

    status = StorageManager(args.data_root).status()
    print(f"SDK Manager cache : {_format_bytes(status.sdkm_cache_bytes)}")
    print(f"Bases             : {_format_bytes(status.base_bytes)}")
    for entry in status.bases:
        print(f"  {entry.label}: {_format_bytes(entry.bytes_used)}")
    print(f"Workspaces        : {_format_bytes(status.workspace_bytes)}")
    for entry in status.workspaces:
        print(f"  {entry.label} ({entry.identifier}): {_format_bytes(entry.bytes_used)}")
    print(f"Build outputs     : {_format_bytes(status.build_output_bytes)}")
    print(f"Tracked total     : {_format_bytes(status.tracked_bytes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
