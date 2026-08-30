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

from .base.construction import (
    BaseBuildResult,
    ensure_jp623_base,
)
from .catalog import TargetResolver, builtin_catalog_paths
from .catalog.resolver import ResolvedCatalogTarget


class PrivilegedBaseError(RuntimeError):
    """Raised when the narrow sudo base-construction child cannot complete."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]
BaseBuilder = Callable[..., BaseBuildResult]

_RESULT_PATH_FIELDS = (
    "target_directory",
    "base_path",
    "lock_path",
    "manifest_path",
    "receipt_path",
)
_RESULT_FIELDS = {
    *_RESULT_PATH_FIELDS,
    "target_lock_digest",
    "base_digest",
    "cache_hit",
}


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in string.hexdigits for character in value)
    )


def _result_payload(result: BaseBuildResult) -> dict[str, object]:
    return {
        "target_directory": str(result.target_directory),
        "base_path": str(result.base_path),
        "lock_path": str(result.lock_path),
        "manifest_path": str(result.manifest_path),
        "receipt_path": str(result.receipt_path),
        "target_lock_digest": result.target_lock_digest,
        "base_digest": result.base_digest,
        "cache_hit": result.cache_hit,
    }


def parse_base_build_result(payload: str, *, data_root: Path) -> BaseBuildResult:
    """Validate the privileged child's JSON before trusting any returned path."""
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PrivilegedBaseError(
            "privileged base builder returned invalid JSON"
        ) from exc
    if not isinstance(data, dict) or set(data) != _RESULT_FIELDS:
        raise PrivilegedBaseError("privileged base builder returned malformed JSON")
    if not isinstance(data["cache_hit"], bool):
        raise PrivilegedBaseError("privileged base builder returned malformed JSON")
    if not _is_sha256(data["target_lock_digest"]) or not _is_sha256(
        data["base_digest"]
    ):
        raise PrivilegedBaseError("privileged base builder returned invalid digests")

    paths: dict[str, Path] = {}
    for field in _RESULT_PATH_FIELDS:
        value = data[field]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise PrivilegedBaseError("privileged base builder returned invalid paths")
        paths[field] = Path(value).resolve()

    root = Path(data_root).expanduser().resolve()
    target_directory = paths["target_directory"]
    try:
        target_directory.relative_to(root / "targets")
    except ValueError as exc:
        raise PrivilegedBaseError(
            "privileged base builder returned a path outside the data root"
        ) from exc
    expected_paths = {
        "base_path": target_directory / "base",
        "lock_path": target_directory / "lock.json",
        "manifest_path": target_directory / "manifest.json",
        "receipt_path": target_directory / "receipt.json",
    }
    if any(paths[field] != expected for field, expected in expected_paths.items()):
        raise PrivilegedBaseError("privileged base builder returned inconsistent paths")

    return BaseBuildResult(
        target_directory=target_directory,
        base_path=paths["base_path"],
        lock_path=paths["lock_path"],
        manifest_path=paths["manifest_path"],
        receipt_path=paths["receipt_path"],
        target_lock_digest=str(data["target_lock_digest"]),
        base_digest=str(data["base_digest"]),
        cache_hit=data["cache_hit"],
    )


def ensure_jp623_base_with_sudo(
    target: ResolvedCatalogTarget,
    *,
    acquisition_receipt_path: Path,
    data_root: Path,
    qemu_binary: Path,
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
) -> BaseBuildResult:
    """Run only JP6.2.3 base construction in the narrow root child."""
    sudo = which("sudo")
    if sudo is None:
        raise PrivilegedBaseError(
            "sudo is not installed; it is required only for new base construction"
        )
    root = Path(data_root).expanduser().resolve()
    receipt = Path(acquisition_receipt_path).expanduser().resolve()
    qemu = Path(qemu_binary).expanduser().resolve()
    python = os.path.abspath(sys.executable)
    command = (
        sudo,
        "--",
        python,
        "-I",
        "-m",
        "orin_stage.privileged_base",
        "--selector",
        target.selector,
        "--data-root",
        str(root),
        "--acquisition-receipt",
        str(receipt),
        "--qemu",
        str(qemu),
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
        raise PrivilegedBaseError("sudo executable could not be started") from exc
    except OSError as exc:
        raise PrivilegedBaseError(
            f"privileged base builder could not start: {exc}"
        ) from exc
    if completed.returncode != 0:
        details = completed.stderr.strip().splitlines()
        detail = details[-1] if details else f"exit {completed.returncode}"
        if detail.startswith("error: "):
            detail = detail.removeprefix("error: ")
        raise PrivilegedBaseError(f"privileged base builder failed: {detail}")
    return parse_base_build_result(completed.stdout, data_root=root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orin_stage.privileged_base")
    parser.add_argument("--selector", required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--acquisition-receipt", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    geteuid: Callable[[], int] | None = None,
    base_builder: BaseBuilder | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    effective_geteuid = os.geteuid if geteuid is None else geteuid
    effective_builder = ensure_jp623_base if base_builder is None else base_builder
    if effective_geteuid() != 0:
        print("error: privileged base builder must run as root", file=sys.stderr)
        return 1

    try:
        paths = builtin_catalog_paths()
        resolver = TargetResolver(paths.targets_dir, paths.schema_path)
        target = resolver.resolve(args.selector)
        result = effective_builder(
            target,
            acquisition_receipt_path=args.acquisition_receipt.expanduser().resolve(),
            data_root=args.data_root.expanduser().resolve(),
            qemu_binary=args.qemu.expanduser().resolve(),
        )
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1

    print(json.dumps(_result_payload(result), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
