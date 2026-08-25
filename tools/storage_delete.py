from __future__ import annotations

import argparse
from pathlib import Path

from orin_stage.storage import (
    DeletionBlockedError,
    DeletionConfirmationRequired,
    StorageManager,
)


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def _show(plan: object) -> None:
    print(f"kind       : {plan.kind}")
    print(f"identifier : {plan.identifier}")
    print(f"path       : {plan.path}")
    print(f"size       : {_format_bytes(plan.bytes_used)}")
    if plan.blocked_by:
        print(f"blocked by : {', '.join(plan.blocked_by)}")
    print(f"confirm    : {plan.identifier}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly delete Orin Stage MVP storage"
    )
    parser.add_argument("data_root", type=Path)
    parser.add_argument("kind", choices=("workspace", "base", "sdkm-cache"))
    parser.add_argument("selector", nargs="?")
    parser.add_argument("--confirm", help="exact identifier printed by the dry-run summary")
    args = parser.parse_args()

    manager = StorageManager(args.data_root)
    if args.kind == "workspace":
        if not args.selector:
            parser.error("workspace deletion requires a workspace name or ID")
        plan = manager.plan_workspace_remove(args.selector)
    elif args.kind == "base":
        if not args.selector:
            parser.error("base deletion requires an exact target lock digest")
        plan = manager.plan_base_remove(args.selector)
    else:
        plan = manager.plan_sdkm_cache_remove()

    _show(plan)
    if args.confirm is None:
        return 0
    if plan.blocked_by:
        raise DeletionBlockedError(
            f"deletion is blocked by: {', '.join(plan.blocked_by)}"
        )

    try:
        if args.kind == "workspace":
            manager.remove_workspace(args.selector, confirmation=args.confirm)
        elif args.kind == "base":
            manager.remove_base(args.selector, confirmation=args.confirm)
        else:
            manager.remove_sdkm_cache(confirmation=args.confirm)
    except DeletionConfirmationRequired as exc:
        parser.error(str(exc))

    print("deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
