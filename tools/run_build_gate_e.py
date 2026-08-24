from __future__ import annotations

import argparse
from pathlib import Path

from orin_stage.build_capsule import BuildCapsuleRunner
from orin_stage.build_identity import JP6_BUILD_IDENTITY
from orin_stage.build_corpus import (
    BuildCorpusError,
    run_same_tree_build_corpus,
    verify_toolchain_archive,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the minimum Gate E same-tree build corpus."
    )
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--toolchain-root", required=True, type=Path)
    parser.add_argument("--toolchain-archive", required=True, type=Path)
    args = parser.parse_args()

    try:
        archive_sha256 = verify_toolchain_archive(args.toolchain_archive)
        runner = BuildCapsuleRunner()
        result = run_same_tree_build_corpus(
            args.workspace_root,
            args.toolchain_root,
            build_runner=runner,
        )
    except BuildCorpusError as exc:
        print(f"gate-e: failed: {exc}")
        return 1

    print("gate-e: passed")
    print(f"BUILD_IMAGE={runner.image}")
    print(f"BUILD_IDENTITY_DIGEST={JP6_BUILD_IDENTITY.digest()}")
    print(
        "NVIDIA_CROSS_PACKAGES="
        + (
            ",".join(
                f"{package.name}={package.version}:{package.architecture}"
                for package in JP6_BUILD_IDENTITY.nvidia_cross_packages
            )
            or "none"
        )
    )
    print(f"TOOLCHAIN_ARCHIVE_SHA256={archive_sha256}")
    print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
