from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .catalog import CatalogError, TargetResolver, builtin_catalog_paths
from .doctor import doctor_exit_code, format_report, run_doctor
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

    parser.print_help()
    return 0
