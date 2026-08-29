from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

from .storage import StorageError, _allocated_tree_bytes, _validate_digest


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _parse_measurement_result(payload: str) -> int:
    try:
        result = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageError(
            "privileged storage measurement returned invalid JSON"
        ) from exc
    if not isinstance(result, dict) or set(result) != {"bytes_used"}:
        raise StorageError("privileged storage measurement returned invalid result")
    bytes_used = result["bytes_used"]
    if isinstance(bytes_used, bool) or not isinstance(bytes_used, int):
        raise StorageError(
            "privileged storage measurement returned invalid byte count"
        )
    if bytes_used < 0:
        raise StorageError(
            "privileged storage measurement returned invalid byte count"
        )
    return bytes_used


def measure_base_storage_with_sudo(
    data_root: Path,
    target_lock_digest: str,
    *,
    sudo_binary: str = "sudo",
    python_binary: str | None = None,
    runner: Runner | None = None,
) -> int:
    """Measure one immutable root-owned target through a narrow sudo child."""

    digest = _validate_digest(target_lock_digest)
    root = Path(data_root).expanduser().resolve()
    if not root.is_dir():
        raise StorageError(f"data root does not exist: {root}")
    interpreter = os.path.abspath(
        sys.executable if python_binary is None else python_binary
    )
    command = (
        sudo_binary,
        "--",
        interpreter,
        "-m",
        "orin_stage.privileged_storage_measure",
        "--data-root",
        str(root),
        "--target-digest",
        digest,
    )
    try:
        completed = (subprocess.run if runner is None else runner)(
            command,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise StorageError("sudo executable could not be started") from exc
    except OSError as exc:
        raise StorageError(
            f"privileged storage measurement could not start: {exc}"
        ) from exc

    if completed.returncode != 0:
        details = (completed.stderr or "").strip().splitlines()
        detail = details[-1] if details else f"exit {completed.returncode}"
        if detail.startswith("error: "):
            detail = detail.removeprefix("error: ")
        raise StorageError(f"privileged storage measurement failed: {detail}")
    return _parse_measurement_result(completed.stdout)


def _target_directory(data_root: Path, target_lock_digest: str) -> Path:
    digest = _validate_digest(target_lock_digest)
    try:
        root = Path(data_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise StorageError(f"cannot resolve data root: {exc}") from exc
    if not root.is_dir():
        raise StorageError(f"data root is not a directory: {root}")

    targets = root / "targets"
    if targets.is_symlink() or not targets.is_dir():
        raise StorageError(f"targets path is not a real directory: {targets}")
    target = targets / digest
    if target.parent != targets or target.is_symlink() or not target.is_dir():
        raise StorageError(f"published base target not found: {digest}")
    try:
        canonical_target = target.resolve(strict=True)
        relative_target = canonical_target.relative_to(targets)
    except (OSError, ValueError) as exc:
        raise StorageError("published base target escapes the data root") from exc
    if relative_target.parts != (digest,):
        raise StorageError("published base target escapes the data root")
    return canonical_target


def _measure_target_storage(
    data_root: Path,
    target_lock_digest: str,
    *,
    geteuid: Callable[[], int],
) -> int:
    if geteuid() != 0:
        raise StorageError("privileged storage measurement must run as root")
    target = _target_directory(data_root, target_lock_digest)
    return _allocated_tree_bytes(target)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orin_stage.privileged_storage_measure"
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--target-digest", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    geteuid: Callable[[], int] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    effective_geteuid = os.geteuid if geteuid is None else geteuid
    try:
        bytes_used = _measure_target_storage(
            args.data_root,
            args.target_digest,
            geteuid=effective_geteuid,
        )
    except (StorageError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {"bytes_used": bytes_used},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
