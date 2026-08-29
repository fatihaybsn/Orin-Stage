from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

from .storage import StorageError, _validate_digest


Runner = Callable[..., subprocess.CompletedProcess[str]]

_RESULT_FIELDS = {"removed", "target_lock_digest"}


def _parse_deletion_result(payload: str, *, expected_digest: str) -> None:
    try:
        result = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageError(
            "privileged storage deletion returned invalid JSON"
        ) from exc
    if not isinstance(result, dict) or set(result) != _RESULT_FIELDS:
        raise StorageError("privileged storage deletion returned invalid result")
    if result["removed"] is not True:
        raise StorageError("privileged storage deletion returned invalid result")
    if result["target_lock_digest"] != expected_digest:
        raise StorageError("privileged storage deletion returned inconsistent digest")


def remove_base_storage_with_sudo(
    data_root: Path,
    target_lock_digest: str,
    *,
    sudo_binary: str = "sudo",
    runner: Runner | None = None,
) -> None:
    """Remove one confirmed immutable target through a narrow root child."""

    digest = _validate_digest(target_lock_digest)
    root = Path(data_root).expanduser().resolve()
    if not root.is_dir():
        raise StorageError(f"data root does not exist: {root}")
    interpreter = os.path.abspath(sys.executable)
    command = (
        sudo_binary,
        "--",
        interpreter,
        "-m",
        "orin_stage.privileged_storage_delete",
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
            f"privileged storage deletion could not start: {exc}"
        ) from exc

    if completed.returncode != 0:
        error_lines = [
            line.removeprefix("error: ")
            for line in (completed.stderr or "").splitlines()
            if line.startswith("error: ")
        ]
        detail = error_lines[-1] if error_lines else f"exit {completed.returncode}"
        raise StorageError(f"privileged storage deletion failed: {detail}")
    _parse_deletion_result(completed.stdout, expected_digest=digest)


def _target_directory(data_root: Path, target_lock_digest: str) -> Path:
    digest = _validate_digest(target_lock_digest)
    requested_root = Path(data_root).expanduser()
    if requested_root.is_symlink():
        raise StorageError(f"data root is not a real directory: {requested_root}")
    try:
        root = requested_root.resolve(strict=True)
    except OSError as exc:
        raise StorageError(f"cannot resolve data root: {exc}") from exc
    if not root.is_dir():
        raise StorageError(f"data root is not a real directory: {root}")

    targets = root / "targets"
    if targets.is_symlink() or not targets.is_dir():
        raise StorageError(f"targets path is not a real directory: {targets}")
    try:
        canonical_targets = targets.resolve(strict=True)
    except OSError as exc:
        raise StorageError(f"cannot resolve targets path: {exc}") from exc
    if canonical_targets != targets:
        raise StorageError(f"targets path is not a real directory: {targets}")

    target = targets / digest
    if target.is_symlink() or not target.is_dir():
        raise StorageError(f"published base target not found: {digest}")
    try:
        canonical_target = target.resolve(strict=True)
        relative_target = canonical_target.relative_to(canonical_targets)
    except (OSError, ValueError) as exc:
        raise StorageError("published base target escapes the data root") from exc
    if relative_target.parts != (digest,):
        raise StorageError("published base target escapes the data root")
    return canonical_target


def _remove_target_storage(
    data_root: Path,
    target_lock_digest: str,
    *,
    geteuid: Callable[[], int],
    remover: Callable[[Path], None] = shutil.rmtree,
) -> None:
    if geteuid() != 0:
        raise StorageError("privileged storage deletion must run as root")
    target = _target_directory(data_root, target_lock_digest)
    try:
        remover(target)
    except OSError as exc:
        raise StorageError(f"cannot remove base target: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orin_stage.privileged_storage_delete"
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--target-digest", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    geteuid: Callable[[], int] | None = None,
    remover: Callable[[Path], None] = shutil.rmtree,
) -> int:
    args = build_parser().parse_args(argv)
    effective_geteuid = os.geteuid if geteuid is None else geteuid
    try:
        _remove_target_storage(
            args.data_root,
            args.target_digest,
            geteuid=effective_geteuid,
            remover=remover,
        )
    except (StorageError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "removed": True,
                "target_lock_digest": args.target_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
