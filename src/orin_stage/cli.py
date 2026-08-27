from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from . import __version__
from .doctor import doctor_exit_code, format_report, run_doctor
from .runtime import resolve_data_root


PROGRAM_NAME = "ostg"
PROGRAM_VERSION = __version__


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

    parser.print_help()
    return 0
