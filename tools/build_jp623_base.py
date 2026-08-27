#!/usr/bin/env python3
"""Developer integration runner for Step 3: official JP6.2.3 base construction.

Acquisition remains a separate user-level step. Run ``tools/acquire_jp623.py``
first, then invoke this builder with the published acquisition receipt. The
builder needs root for official BSP/rootfs extraction, mounts and ARM64 chroot.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orin_stage.base import ensure_jp623_base  # noqa: E402
from orin_stage.catalog import (  # noqa: E402
    TargetResolver,
    builtin_catalog_paths,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Absolute ORIN_STAGE_DATA directory",
    )
    parser.add_argument(
        "--acquisition-receipt",
        type=Path,
        required=True,
        help="Published Step 2 acquisition receipt.json",
    )
    parser.add_argument(
        "--qemu",
        type=Path,
        default=Path("/usr/bin/qemu-aarch64-static"),
        help="Host qemu-aarch64-static binary",
    )
    args = parser.parse_args()

    catalog_paths = builtin_catalog_paths()
    resolver = TargetResolver(catalog_paths.targets_dir, catalog_paths.schema_path)
    target = resolver.resolve("jetson-orin@jp6.2.3")
    result = ensure_jp623_base(
        target,
        acquisition_receipt_path=args.acquisition_receipt.expanduser().resolve(),
        data_root=args.data_root.expanduser().resolve(),
        qemu_binary=args.qemu.expanduser().resolve(),
    )

    print(f"base:        {'cache-hit' if result.cache_hit else 'constructed+validated'}")
    print(f"base digest: {result.base_digest}")
    print(f"target lock: {result.target_lock_digest}")
    print(f"rootfs:      {result.base_path}")
    print(f"receipt:     {result.receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
