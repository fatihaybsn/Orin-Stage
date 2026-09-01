#!/usr/bin/env python3
"""Write the private-native-wheel CPython minor dependency substvar."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


def python_depends(major: int, minor: int) -> str:
    return f"python3 (>= {major}.{minor}~), python3 (<< {major}.{minor + 1})"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    major, minor = sys.version_info[:2]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    values: dict[str, str] = {}
    if args.output.exists():
        for line in args.output.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                raise ValueError(f"invalid substvars line in {args.output}: {line!r}")
            key, value = line.split("=", 1)
            values[key] = value
    values["orin:PythonDepends"] = python_depends(major, minor)
    args.output.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(values.items())),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
