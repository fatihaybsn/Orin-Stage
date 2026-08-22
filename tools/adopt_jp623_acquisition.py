#!/usr/bin/env python3
"""Adopt an existing SDK Manager JP6.2.3 download into Orin Stage."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orin_stage.acquisition.sdk_manager import SdkManagerClient  # noqa: E402
from orin_stage.acquisition.sdk_manager_adoption import (  # noqa: E402
    adopt_sdk_manager_acquisition,
)
from orin_stage.catalog.resolver import TargetResolver  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--existing-download-folder", type=Path, required=True)
    parser.add_argument(
        "--sdk-manager-state-root",
        type=Path,
        help="Optional SDK Manager metadata root (defaults to ~/.nvsdkm)",
    )
    args = parser.parse_args()

    resolver = TargetResolver(
        targets_dir=REPO_ROOT / "catalog" / "targets",
        schema_path=REPO_ROOT / "catalog" / "schema" / "target.schema.json",
    )
    target = resolver.resolve("jetson-orin@jp6.2.3")
    result = adopt_sdk_manager_acquisition(
        SdkManagerClient(),
        target,
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        data_root=args.data_root.expanduser().resolve(),
        existing_download_folder=args.existing_download_folder.expanduser().resolve(),
        sdk_manager_state_root=(
            args.sdk_manager_state_root.expanduser().resolve()
            if args.sdk_manager_state_root is not None
            else None
        ),
    )

    print(f"acquisition: {'cache-hit' if result.cache_hit else 'adopted+verified'}")
    print(f"digest:      {result.acquisition_digest}")
    print(f"response:    {result.response_file.path}")
    print(f"receipt:     {result.receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
