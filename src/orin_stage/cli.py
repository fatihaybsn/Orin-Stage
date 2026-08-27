from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .acquisition.sdk_manager import SdkManagerClient
from .catalog import CatalogError, TargetResolver, builtin_catalog_paths
from .catalog.resolver import ResolvedCatalogTarget
from .doctor import doctor_exit_code, format_report, run_doctor
from .planning.orchestration import (
    JP623_HARDWARE_PROFILE,
    JP623_QEMU_BINARY,
    JP623_SDK_MANAGER_TARGET,
    ReleaseEnsureResult,
    ensure_jp623_release,
)
from .planning.planner import BasePlanStatus
from .privileged_base import ensure_jp623_base_with_sudo
from .runtime import resolve_data_root


PROGRAM_NAME = "ostg"
PROGRAM_VERSION = __version__


def _semantic_version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _format_target_list() -> str:
    paths = builtin_catalog_paths()
    resolver = TargetResolver(paths.targets_dir, paths.schema_path)
    targets = sorted(
        resolver.list_targets(),
        key=lambda target: _semantic_version_key(target.jetpack_version),
    )
    rows = [
        ("TARGET", "JETPACK", "L4T", "STATUS"),
        *(
            (
                target.primary_selector,
                target.jetpack_version,
                target.jetson_linux_version,
                target.support_status,
            )
            for target in targets
        ),
    ]
    widths = [max(len(row[index]) for row in rows) for index in range(4)]
    return "\n".join(
        "  ".join(
            value.ljust(widths[index]) for index, value in enumerate(row)
        ).rstrip()
        for row in rows
    )


def _run_target_list() -> int:
    try:
        output = _format_target_list()
    except CatalogError as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: cannot load built-in target catalog: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


def _validate_target_ensure_status(
    target: ResolvedCatalogTarget,
    *,
    allow_validation_pending: bool,
) -> None:
    if target.is_unavailable:
        raise RuntimeError(
            f"target {target.selector!r} is unavailable and cannot be ensured"
        )
    version = str(target.record["release"]["jetpack"]["version"])
    if version != "6.2.3":
        raise RuntimeError("target ensure is currently implemented only for JP6.2.3")
    if target.is_validation_pending and not allow_validation_pending:
        raise RuntimeError(
            f"target {target.selector!r} is validation-pending; "
            "pass --allow-validation-pending to continue explicitly"
        )
    if not target.is_supported and not target.is_validation_pending:
        raise RuntimeError(
            f"target {target.selector!r} has unsupported status "
            f"{target.support_status!r}"
        )


def _format_target_ensure_result(result: ReleaseEnsureResult) -> str:
    target = result.target
    status = target.support_status
    if target.is_validation_pending:
        status = f"{status} (explicitly allowed)"
    acquisition = result.acquisition_result
    acquisition_status = (
        "cache-hit"
        if acquisition is None or acquisition.cache_hit
        else "downloaded+verified"
    )

    if result.final_plan.base_status is BasePlanStatus.BASE_REUSE:
        base_status = "reused"
        base_digest = result.final_plan.base_digest
        reference = result.final_plan.base_reference
        base_path = Path(reference) / "base" if reference is not None else None
    else:
        if result.base_result is None:
            raise RuntimeError("base construction returned no result")
        base_status = (
            "reused" if result.base_result.cache_hit else "constructed+validated"
        )
        base_digest = result.base_result.base_digest
        base_path = result.base_result.base_path

    if base_digest is None or base_path is None:
        raise RuntimeError("release ensure returned incomplete base evidence")
    rows = (
        ("Target:", target.selector),
        ("Status:", status),
        ("Acquisition:", acquisition_status),
        ("Base:", base_status),
        ("Base digest:", base_digest),
        ("Base path:", str(base_path)),
    )
    width = max(len(label) for label, _value in rows)
    return "\n".join(f"{label:<{width}}  {value}" for label, value in rows)


def _run_target_ensure(
    selector: str,
    *,
    allow_validation_pending: bool,
    data_root: Path,
) -> int:
    if os.geteuid() == 0:
        print("error: Run target ensure as your normal user.", file=sys.stderr)
        print(
            "Orin Stage requests sudo only when base construction is required.",
            file=sys.stderr,
        )
        return 1

    try:
        paths = builtin_catalog_paths()
        resolver = TargetResolver(paths.targets_dir, paths.schema_path)
        target = resolver.resolve(selector)
        _validate_target_ensure_status(
            target,
            allow_validation_pending=allow_validation_pending,
        )
        result = ensure_jp623_release(
            resolver,
            SdkManagerClient(),
            selector=selector,
            hardware_profile=JP623_HARDWARE_PROFILE,
            required_sdk_manager_target=JP623_SDK_MANAGER_TARGET,
            data_root=data_root,
            qemu_binary=JP623_QEMU_BINARY,
            base_builder=ensure_jp623_base_with_sudo,
        )
        output = _format_target_ensure_result(result)
    except (RuntimeError, ValueError, OSError) as exc:
        detail = str(exc).splitlines()[0]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    print(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Orin Stage — JetPack 6 target software workspace engine",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PROGRAM_VERSION}",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        metavar="PATH",
        help="override the persistent data root (default: ~/.local/share/orin-stage)",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor",
        help="check host prerequisites without changing the host",
    )
    target_parser = subparsers.add_parser(
        "target",
        help="inspect target software releases",
    )
    target_subparsers = target_parser.add_subparsers(
        dest="target_command",
        required=True,
    )
    target_subparsers.add_parser(
        "list",
        help="list built-in GA target releases",
    )
    ensure_parser = target_subparsers.add_parser(
        "ensure",
        help="ensure the implemented target acquisition and immutable base",
    )
    ensure_parser.add_argument("selector", metavar="SELECTOR")
    ensure_parser.add_argument(
        "--allow-validation-pending",
        action="store_true",
        help="explicitly allow a validation-pending target",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve once so every subcommand receives one canonical absolute path.
    # Resolution deliberately does not create directories.
    data_root = resolve_data_root(args.data_root)

    if args.command == "doctor":
        checks = run_doctor(data_root)
        print(format_report(checks))
        return doctor_exit_code(checks)

    if args.command == "target" and args.target_command == "list":
        return _run_target_list()

    if args.command == "target" and args.target_command == "ensure":
        return _run_target_ensure(
            args.selector,
            allow_validation_pending=args.allow_validation_pending,
            data_root=data_root,
        )

    parser.print_help()
    return 0
