from __future__ import annotations

import argparse
import json
import os
import shutil
import string
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

from .materialization_seed import (
    MaterializationSeedResult,
    create_materialization_seed,
)


class PrivilegedMaterializationError(RuntimeError):
    """Raised when narrow privileged seed creation cannot complete."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]
SeedCreator = Callable[[Path], MaterializationSeedResult]

_RESULT_FIELDS = {"archive_path", "metadata_path", "seed_sha256"}


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in string.hexdigits for character in value)
    )


def _canonical_target(data_root: Path, target_dir: Path) -> tuple[Path, Path]:
    root = Path(data_root).expanduser().resolve()
    requested = Path(target_dir).expanduser()
    if requested.is_symlink():
        raise PrivilegedMaterializationError(
            "materialization target must not be a symlink"
        )
    target = requested.resolve()
    if not root.is_dir():
        raise PrivilegedMaterializationError(f"data root does not exist: {root}")
    if target.parent != root / "targets" or not target.is_dir():
        raise PrivilegedMaterializationError(
            f"target directory is not under {root / 'targets'}: {target}"
        )
    return root, target


def _result_payload(result: MaterializationSeedResult) -> dict[str, str]:
    return {
        "archive_path": str(result.archive_path),
        "metadata_path": str(result.metadata_path),
        "seed_sha256": result.seed_sha256,
    }


def parse_materialization_seed_result(
    payload: str,
    *,
    target_dir: Path,
) -> MaterializationSeedResult:
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PrivilegedMaterializationError(
            "privileged materialization builder returned invalid JSON"
        ) from exc
    if not isinstance(data, dict) or set(data) != _RESULT_FIELDS:
        raise PrivilegedMaterializationError(
            "privileged materialization builder returned malformed JSON"
        )
    if not _is_sha256(data["seed_sha256"]):
        raise PrivilegedMaterializationError(
            "privileged materialization builder returned an invalid digest"
        )

    target = Path(target_dir).expanduser().resolve()
    expected_archive = target / "materialization" / "seed.tar"
    expected_metadata = target / "materialization" / "seed.json"
    archive = data["archive_path"]
    metadata = data["metadata_path"]
    if (
        not isinstance(archive, str)
        or not Path(archive).is_absolute()
        or Path(archive).resolve() != expected_archive
        or not isinstance(metadata, str)
        or not Path(metadata).is_absolute()
        or Path(metadata).resolve() != expected_metadata
    ):
        raise PrivilegedMaterializationError(
            "privileged materialization builder returned inconsistent paths"
        )
    return MaterializationSeedResult(
        archive_path=expected_archive,
        metadata_path=expected_metadata,
        seed_sha256=str(data["seed_sha256"]),
    )


def create_materialization_seed_with_sudo(
    target_dir: Path,
    *,
    data_root: Path,
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
) -> MaterializationSeedResult:
    """Create only the missing materialization seed in a narrow root child."""
    root, target = _canonical_target(data_root, target_dir)
    sudo = which("sudo")
    if sudo is None:
        raise PrivilegedMaterializationError(
            "sudo is not installed; it is required only for materialization seed creation"
        )
    python = os.path.abspath(sys.executable)
    command = (
        sudo,
        "--",
        python,
        "-I",
        "-m",
        "orin_stage.privileged_materialization",
        "--data-root",
        str(root),
        "--target-dir",
        str(target),
    )
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise PrivilegedMaterializationError(
            "sudo executable could not be started"
        ) from exc
    except OSError as exc:
        raise PrivilegedMaterializationError(
            f"privileged materialization builder could not start: {exc}"
        ) from exc
    if completed.returncode != 0:
        details = completed.stderr.strip().splitlines()
        detail = details[-1] if details else f"exit {completed.returncode}"
        if detail.startswith("error: "):
            detail = detail.removeprefix("error: ")
        raise PrivilegedMaterializationError(
            f"privileged materialization builder failed: {detail}"
        )
    return parse_materialization_seed_result(
        completed.stdout,
        target_dir=target,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orin_stage.privileged_materialization"
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    geteuid: Callable[[], int] | None = None,
    seed_creator: SeedCreator | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    effective_geteuid = os.geteuid if geteuid is None else geteuid
    effective_creator = (
        create_materialization_seed if seed_creator is None else seed_creator
    )
    if effective_geteuid() != 0:
        print(
            "error: privileged materialization builder must run as root",
            file=sys.stderr,
        )
        return 1

    try:
        _root, target = _canonical_target(args.data_root, args.target_dir)
        result = effective_creator(target)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1

    print(json.dumps(_result_payload(result), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
