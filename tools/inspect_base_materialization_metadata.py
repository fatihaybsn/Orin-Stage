#!/usr/bin/env python3
"""Read-only metadata inventory for a published base materialization."""

from __future__ import annotations

import argparse
import errno
import os
import stat
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


RUNTIME_TREES = ("proc", "sys", "dev")
ACL_XATTRS = {"system.posix_acl_access", "system.posix_acl_default"}
XATTR_CATEGORIES = (
    "user.*",
    "security.capability",
    "other security.*",
    "trusted.*",
    "other system.*",
)
SPECIAL_KINDS = ("block device", "character device", "FIFO", "socket")
EXAMPLE_LIMIT = 3


@dataclass
class Inventory:
    paths: int = 0
    uids: set[int] = field(default_factory=set)
    gids: set[int] = field(default_factory=set)
    acl_paths: set[str] = field(default_factory=set)
    xattrs: dict[str, dict[str, set[str]]] = field(
        default_factory=lambda: {
            category: defaultdict(set) for category in XATTR_CATEGORIES
        }
    )
    hardlink_candidates: dict[tuple[int, int], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    symlinks: int = 0
    sparse_files: set[str] = field(default_factory=set)
    special_files: dict[str, set[str]] = field(
        default_factory=lambda: {kind: set() for kind in SPECIAL_KINDS}
    )
    warnings: list[str] = field(default_factory=list)


def _display_path(base: Path, path: Path) -> str:
    relative = path.relative_to(base)
    return "/" if relative == Path(".") else f"/{relative}"


def _xattr_category(name: str) -> str | None:
    if name.startswith("user."):
        return "user.*"
    if name == "security.capability":
        return "security.capability"
    if name.startswith("security."):
        return "other security.*"
    if name.startswith("trusted."):
        return "trusted.*"
    if name.startswith("system."):
        return "other system.*"
    return None


def _record_path(
    inventory: Inventory,
    base: Path,
    path: Path,
    metadata: os.stat_result,
) -> None:
    display = _display_path(base, path)
    mode = metadata.st_mode
    inventory.paths += 1
    inventory.uids.add(metadata.st_uid)
    inventory.gids.add(metadata.st_gid)

    if stat.S_ISLNK(mode):
        inventory.symlinks += 1
    elif not stat.S_ISDIR(mode) and metadata.st_nlink > 1:
        inventory.hardlink_candidates[(metadata.st_dev, metadata.st_ino)].append(display)

    if stat.S_ISREG(mode) and metadata.st_blocks * 512 < metadata.st_size:
        inventory.sparse_files.add(display)

    special_kind = None
    if stat.S_ISBLK(mode):
        special_kind = "block device"
    elif stat.S_ISCHR(mode):
        special_kind = "character device"
    elif stat.S_ISFIFO(mode):
        special_kind = "FIFO"
    elif stat.S_ISSOCK(mode):
        special_kind = "socket"
    if special_kind is not None:
        inventory.special_files[special_kind].add(display)

    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        if exc.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            inventory.warnings.append(f"{display}: cannot list xattrs: {exc}")
        return

    if ACL_XATTRS.intersection(names):
        inventory.acl_paths.add(display)
    for name in names:
        category = _xattr_category(name)
        if category is not None:
            inventory.xattrs[category][display].add(name)


def _scan(base: Path, root: Path, *, skip_runtime_trees: bool = False) -> Inventory:
    inventory = Inventory()
    pending = [root]

    while pending:
        path = pending.pop()
        display = _display_path(base, path)
        try:
            metadata = path.lstat()
        except OSError as exc:
            inventory.warnings.append(f"{display}: cannot stat: {exc}")
            continue

        _record_path(inventory, base, path, metadata)
        if not stat.S_ISDIR(metadata.st_mode):
            continue

        try:
            entries = sorted(os.scandir(path), key=lambda entry: entry.name, reverse=True)
        except OSError as exc:
            inventory.warnings.append(f"{display}: cannot scan directory: {exc}")
            continue

        for entry in entries:
            if skip_runtime_trees and path == root and entry.name in RUNTIME_TREES:
                continue
            pending.append(Path(entry.path))

    return inventory


def _examples(paths: set[str] | list[str]) -> str:
    selected = sorted(paths)[:EXAMPLE_LIMIT]
    return ", ".join(selected) if selected else "-"


def _id_summary(values: set[int]) -> str:
    if not values:
        return "none"
    ordered = sorted(values)
    rendered = ", ".join(str(value) for value in ordered)
    return f"{rendered} (count {len(ordered)}, min {ordered[0]}, max {ordered[-1]})"


def _hardlink_groups(inventory: Inventory) -> list[list[str]]:
    groups = [paths for paths in inventory.hardlink_candidates.values() if len(paths) > 1]
    return sorted((sorted(paths) for paths in groups), key=lambda paths: paths[0])


def _render_inventory(title: str, inventory: Inventory) -> None:
    print(f"\n{title} ({inventory.paths} paths)")
    print(f"  UIDs: {_id_summary(inventory.uids)}")
    print(f"  GIDs: {_id_summary(inventory.gids)}")
    print(
        f"  POSIX ACL: {len(inventory.acl_paths)} paths; "
        f"examples: {_examples(inventory.acl_paths)}"
    )
    print("  Xattrs (path count / attribute count):")
    for category in XATTR_CATEGORIES:
        paths = inventory.xattrs[category]
        attribute_count = sum(len(names) for names in paths.values())
        print(
            f"    {category}: {len(paths)} / {attribute_count}; "
            f"examples: {_examples(set(paths))}"
        )

    groups = _hardlink_groups(inventory)
    group_examples = "; ".join(
        "[" + ", ".join(paths[:EXAMPLE_LIMIT]) + "]"
        for paths in groups[:EXAMPLE_LIMIT]
    )
    print(f"  Hardlink groups: {len(groups)}; examples: {group_examples or '-'}")
    print(f"  Symlinks: {inventory.symlinks}")
    print(
        f"  Sparse regular files: {len(inventory.sparse_files)}; "
        f"examples: {_examples(inventory.sparse_files)}"
    )
    print("  Special files:")
    for kind in SPECIAL_KINDS:
        paths = inventory.special_files[kind]
        print(f"    {kind}: {len(paths)}; examples: {_examples(paths)}")


def _validate_target_dir(
    parser: argparse.ArgumentParser,
    target_dir: Path,
) -> tuple[Path, Path, Path]:
    if not target_dir.is_dir():
        parser.error(f"target directory does not exist: {target_dir}")
    base = target_dir / "base"
    manifest = target_dir / "manifest.json"
    receipt = target_dir / "receipt.json"
    if not base.is_dir():
        parser.error(f"base directory does not exist: {base}")
    for metadata_file in (manifest, receipt):
        if not metadata_file.is_file():
            parser.error(f"metadata file does not exist: {metadata_file}")
    return base, manifest, receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect a published base without modifying it."
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        required=True,
        help="ORIN_STAGE_DATA/targets/<target-digest> directory",
    )
    args = parser.parse_args()
    target_dir = args.target_dir.expanduser().resolve()
    base, manifest, receipt = _validate_target_dir(parser, target_dir)

    print("Base materialization metadata (read-only)")
    print(f"target:   {target_dir}")
    print(f"base:     {base}")
    print(f"manifest: {manifest}")
    print(f"receipt:  {receipt}")

    inventories: list[Inventory] = []
    normal = _scan(base, base, skip_runtime_trees=True)
    inventories.append(normal)
    _render_inventory("Normal filesystem (excluding /proc, /sys, /dev)", normal)

    print("\nRuntime trees (reported separately)")
    for name in RUNTIME_TREES:
        runtime_root = base / name
        if not os.path.lexists(runtime_root):
            print(f"\n/{name}: not present")
            continue
        runtime = _scan(base, runtime_root)
        inventories.append(runtime)
        _render_inventory(f"/{name}", runtime)

    warnings = [warning for inventory in inventories for warning in inventory.warnings]
    if warnings:
        print(f"\nScan warnings: {len(warnings)}", file=sys.stderr)
        for warning in warnings[:EXAMPLE_LIMIT]:
            print(f"  {warning}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
