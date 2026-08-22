from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .receipt import base_directory_is_reusable


JP623_CANONICAL_ID = "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
CONSTRUCTION_LEASE = ".orin-stage-construction.json"
_STAGING_NAME = re.compile(r"^\.base-jp623-[A-Za-z0-9_-]+$")


class BaseAttemptCleanupError(RuntimeError):
    """Raised when stale Step 3 output cannot be inspected or removed safely."""


@dataclass(frozen=True, slots=True)
class CleanupEntry:
    kind: str
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class CleanupInspection:
    removable: tuple[CleanupEntry, ...]
    protected: tuple[CleanupEntry, ...]


def _process_start_time(pid: int) -> str | None:
    try:
        content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    closing = content.rfind(")")
    if closing < 0:
        return None
    fields = content[closing + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def write_construction_lease(staging: Path) -> Path:
    directory = Path(staging)
    if not directory.is_dir() or directory.is_symlink():
        raise BaseAttemptCleanupError(f"construction staging must be a real directory: {directory}")
    start_time = _process_start_time(os.getpid())
    if start_time is None:
        raise BaseAttemptCleanupError("cannot determine construction process start time")
    path = directory / CONSTRUCTION_LEASE
    payload = {
        "schema_version": 1,
        "pid": os.getpid(),
        "process_start_time": start_time,
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _has_active_lease(directory: Path) -> bool:
    path = directory / CONSTRUCTION_LEASE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        expected = str(payload["process_start_time"])
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return False
    return pid > 0 and _process_start_time(pid) == expected


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _has_live_process_reference(directory: Path) -> bool:
    resolved = directory.resolve()
    proc = Path("/proc")
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        references = [process / "cwd", process / "root"]
        fd_directory = process / "fd"
        try:
            references.extend(fd_directory.iterdir())
        except OSError:
            pass
        for reference in references:
            try:
                target = Path(os.readlink(reference))
            except OSError:
                continue
            if target.is_absolute() and (target == resolved or _is_within(target, resolved)):
                return True
    return False


def _has_active_builder_for_data_root(data_root: Path) -> bool:
    expected_root = str(data_root.resolve()).encode()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            arguments = (process / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not any(argument.endswith(b"build_jp623_base.py") for argument in arguments):
            continue
        if expected_root in arguments:
            return True
    return False


def _load_json_object(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _target_canonical_id(directory: Path) -> str | None:
    lock_path = directory / "lock.json"
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        lock = json.loads(raw)
        target = lock.get("target") if isinstance(lock, dict) else None
        canonical_id = target.get("canonical_id") if isinstance(target, dict) else None
        if isinstance(canonical_id, str):
            return canonical_id
    except json.JSONDecodeError:
        pass
    if re.search(rf'"canonical_id"\s*:\s*"{re.escape(JP623_CANONICAL_ID)}"', raw):
        return JP623_CANONICAL_ID
    return None


def _publication_problems(directory: Path) -> tuple[str, ...]:
    problems: list[str] = []
    if not (directory / "base").is_dir():
        problems.append("missing base/")
    for name in ("lock.json", "manifest.json", "receipt.json"):
        path = directory / name
        if not path.is_file():
            problems.append(f"missing {name}")
        elif _load_json_object(path) is None:
            problems.append(f"malformed {name}")
    if not problems:
        problems.append("base_directory_is_reusable() rejected publication")
    return tuple(problems)


def inspect_jp623_base_attempts(data_root: Path) -> CleanupInspection:
    root = Path(data_root).expanduser().resolve()
    if not root.is_absolute() or root == Path(root.anchor):
        raise BaseAttemptCleanupError("data root must be a specific absolute directory")
    removable: list[CleanupEntry] = []
    protected: list[CleanupEntry] = []

    staging_root = root / "staging"
    builder_active = _has_active_builder_for_data_root(root)
    if staging_root.is_dir() and not staging_root.is_symlink():
        for directory in sorted(staging_root.iterdir()):
            if not _STAGING_NAME.fullmatch(directory.name):
                continue
            if directory.is_symlink() or directory.is_mount() or not directory.is_dir():
                protected.append(CleanupEntry("staging", directory, "not a real directory"))
            elif (
                builder_active
                or _has_active_lease(directory)
                or _has_live_process_reference(directory)
            ):
                protected.append(CleanupEntry("staging", directory, "active construction process"))
            else:
                removable.append(CleanupEntry("staging", directory, "stale JP6.2.3 construction staging"))

    targets_root = root / "targets"
    if targets_root.is_dir() and not targets_root.is_symlink():
        for directory in sorted(targets_root.iterdir()):
            if directory.is_symlink() or directory.is_mount() or not directory.is_dir():
                continue
            if base_directory_is_reusable(directory):
                protected.append(CleanupEntry("target", directory, "valid reusable published base"))
                continue
            canonical_id = _target_canonical_id(directory)
            if canonical_id != JP623_CANONICAL_ID:
                continue
            removable.append(
                CleanupEntry("target", directory, "; ".join(_publication_problems(directory)))
            )

    return CleanupInspection(tuple(removable), tuple(protected))


def remove_jp623_base_attempts(data_root: Path) -> tuple[CleanupEntry, ...]:
    root = Path(data_root).expanduser().resolve()
    inspection = inspect_jp623_base_attempts(root)
    removed: list[CleanupEntry] = []
    for entry in inspection.removable:
        current = inspect_jp623_base_attempts(root)
        current_by_path = {item.path: item for item in current.removable}
        fresh = current_by_path.get(entry.path)
        if fresh is None:
            continue
        expected_parent = root / ("staging" if fresh.kind == "staging" else "targets")
        if (
            expected_parent.is_symlink()
            or fresh.path.parent != expected_parent
            or fresh.path.is_symlink()
            or fresh.path.is_mount()
        ):
            raise BaseAttemptCleanupError(f"cleanup target changed during inspection: {fresh.path}")
        shutil.rmtree(fresh.path)
        removed.append(fresh)
    return tuple(removed)
