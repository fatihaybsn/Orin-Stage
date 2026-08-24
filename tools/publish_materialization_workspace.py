#!/usr/bin/env python3
"""Publish a parity-validated staging tree as a minimal workspace."""

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
)
from orin_stage.workspace_publish import (  # noqa: E402
    WorkspacePublishError,
    publish_materialization_workspace,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish a validated materialization staging tree atomically."
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument(
        "--staging-root",
        type=Path,
        help="Reuse and revalidate an existing staging/<uuid>/root without extraction",
    )
    args = parser.parse_args()

    try:
        result = publish_materialization_workspace(
            args.data_root,
            args.target_dir,
            args.workspace_name,
            staging_root=args.staging_root,
        )
    except (WorkspacePublishError, MaterializationExtractionError) as exc:
        parser.exit(1, f"error: {exc}\n")

    action = "revalidated+published" if result.reused_staging else "extracted+published"
    print(f"workspace: {action}")
    print(f"workspace id: {result.workspace_id}")
    print(f"workspace path: {result.workspace_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
