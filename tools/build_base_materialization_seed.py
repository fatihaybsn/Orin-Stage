#!/usr/bin/env python3
"""Create a minimal GNU tar seed from a validated immutable base."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orin_stage.materialization_seed import (  # noqa: E402
    MaterializationSeedError,
    create_materialization_seed,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a GNU tar materialization seed from an immutable base."
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        required=True,
        help="ORIN_STAGE_DATA/targets/<target-digest> directory",
    )
    args = parser.parse_args()

    try:
        result = create_materialization_seed(args.target_dir)
    except MaterializationSeedError as exc:
        parser.exit(1, f"error: {exc}\n")

    print(f"archive: {result.archive_path}")
    print(f"metadata: {result.metadata_path}")
    print(f"sha256: {result.seed_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
