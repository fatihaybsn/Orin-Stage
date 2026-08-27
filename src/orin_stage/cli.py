from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from . import __version__
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve now so every later subcommand receives one canonical absolute
    # path. Foundation-only: this deliberately does not create directories.
    resolve_data_root(args.data_root)

    parser.print_help()
    return 0
