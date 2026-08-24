#!/usr/bin/env python3
"""Extract and validate a seed in a temporary rootless Podman staging tree."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orin_stage.materialization_extract import (  # noqa: E402
    MaterializationExtractionError,
    extract_materialization_seed,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract a materialization seed with podman unshare and validate parity."
        )
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Absolute ORIN_STAGE_DATA directory",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        required=True,
        help="ORIN_STAGE_DATA/targets/<target-digest> directory",
    )
    args = parser.parse_args()

    try:
        report = extract_materialization_seed(args.data_root, args.target_dir)
    except MaterializationExtractionError as exc:
        parser.exit(1, f"error: {exc}\n")

    print(f"paths:     {report.path_count}")
    print(f"UID parity: {report.uid_parity}/{report.path_count}")
    print(f"GID parity: {report.gid_parity}/{report.path_count}")
    print(f"mode parity: {report.mode_parity}/{report.path_count}")
    print(f"hardlink parity: {report.hardlink_parity} relationships")
    print(f"symlink parity: {report.symlink_parity} paths")
    print(f"staging:   {report.staging_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
