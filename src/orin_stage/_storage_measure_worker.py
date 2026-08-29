from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Sequence

from .storage import StorageError, _allocated_tree_bytes


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("error: storage measurement worker requires one path", file=sys.stderr)
        return 2

    try:
        bytes_used = _allocated_tree_bytes(Path(arguments[0]))
    except StorageError as exc:
        print(f"error: {str(exc).splitlines()[0]}", file=sys.stderr)
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
