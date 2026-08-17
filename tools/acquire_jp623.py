#!/usr/bin/env python3
"""Developer integration runner for the JP6.2.3 / Orin NX vertical slice.

This is intentionally not the final product CLI. It exists so Step 2 can be
validated against a real NVIDIA SDK Manager installation before the broader
`ostg target ensure` command is implemented.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orin_stage.acquisition.sdk_manager import SdkManagerClient  # noqa: E402
from orin_stage.acquisition.sdk_manager_acquisition import (  # noqa: E402
    ensure_sdk_manager_acquisition,
)
from orin_stage.catalog.resolver import TargetResolver  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Absolute ORIN_STAGE_DATA directory",
    )
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve()
    resolver = TargetResolver(
        targets_dir=REPO_ROOT / "catalog" / "targets",
        schema_path=REPO_ROOT / "catalog" / "schema" / "target.schema.json",
    )
    target = resolver.resolve("jetson-orin@jp6.2.3")

    result = ensure_sdk_manager_acquisition(
        SdkManagerClient(),
        target,
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        data_root=data_root,
    )

    print(f"acquisition: {'cache-hit' if result.cache_hit else 'downloaded+verified'}")
    print(f"digest:      {result.acquisition_digest}")
    print(f"response:    {result.response_file.path}")
    print(f"receipt:     {result.receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
