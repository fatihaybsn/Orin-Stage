from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import re
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping

from .materialization_extract import extract_materialization_seed
from .workspace_publish import (
    WORKSPACE_FORMAT_VERSION,
    WorkspacePublishError,
    publish_materialization_workspace,
)


AT_FDCWD = -100
RENAME_EXCHANGE = 2
_WORKSPACE_STAGING = re.compile(r"[0-9a-f]{32}")
_REMOVE_STAGING = re.compile(r"\.workspace-remove-[0-9a-f]+-[0-9a-f]{32}")


class WorkspaceManagerError(RuntimeError):
    """Base error for workspace lifecycle operations."""


class WorkspaceNotFoundError(WorkspaceManagerError):
    """Raised when a workspace selector does not resolve to a workspace."""


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    workspace_id: str
    workspace_name: str
    target_lock_digest: str
    base_digest: str
    generation: int
    workspace_path: Path
    root_path: Path


def _load_json_object(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceManagerError(f"cannot read workspace metadata {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspaceManagerError(f"workspace metadata is not a JSON object: {path}")
    return value


def _required_string(metadata: Mapping[str, object], field: str) -> str:
    value = metadata.get(field)
    if not isinstance(value, str) or not value:
        raise WorkspaceManagerError(f"workspace metadata has invalid {field!r}")
    return value


def _required_digest(metadata: Mapping[str, object], field: str) -> str:
    value = _required_string(metadata, field)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise WorkspaceManagerError(f"workspace metadata has invalid {field!r}")
    return value


def _workspace_metadata(record: WorkspaceRecord, generation: int) -> dict[str, object]:
    return {
        "format_version": WORKSPACE_FORMAT_VERSION,
        "workspace_id": record.workspace_id,
        "workspace_name": record.workspace_name,
        "target_lock_digest": record.target_lock_digest,
        "base_digest": record.base_digest,
        "generation": generation,
    }


def _write_json_exclusive(path: Path, value: Mapping[str, object]) -> None:
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise WorkspaceManagerError(f"cannot write workspace metadata {path}: {exc}") from exc


def _rename_exchange(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise WorkspaceManagerError("atomic workspace reset requires Linux renameat2") from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_EXCHANGE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EXDEV:
        raise WorkspaceManagerError("staging and workspaces must be on the same filesystem")
    raise WorkspaceManagerError(
        f"atomic workspace reset failed: {os.strerror(error_number)}"
    )


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _remove_tree_in_namespace(
    path: Path,
    *,
    podman_binary: str = "podman",
    runner: Runner = subprocess.run,
) -> None:
    target = Path(path).resolve()
    try:
        completed = runner(
            (podman_binary, "unshare", "rm", "-rf", "--", str(target)),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise WorkspaceManagerError(f"Podman executable not found: {podman_binary!r}") from exc
    if completed.returncode != 0:
        raise WorkspaceManagerError(
            f"cannot remove workspace tree {target}: {completed.stderr or completed.stdout}"
        )


@dataclass(frozen=True, slots=True)
class WorkspaceManager:
    data_root: Path
    podman_binary: str = "podman"

    def __post_init__(self) -> None:
        root = Path(self.data_root).expanduser().resolve()
        if not root.is_dir():
            raise WorkspaceManagerError(f"data root does not exist: {root}")
        object.__setattr__(self, "data_root", root)

    @property
    def workspaces_dir(self) -> Path:
        return self.data_root / "workspaces"

    @property
    def staging_dir(self) -> Path:
        return self.data_root / "staging"

    def create(self, target_dir: Path, workspace_name: str) -> WorkspaceRecord:
        with self._lifecycle_lock():
            try:
                result = publish_materialization_workspace(
                    self.data_root,
                    target_dir,
                    workspace_name,
                )
            except WorkspacePublishError as exc:
                raise WorkspaceManagerError(str(exc)) from exc
            return self._load_workspace(result.workspace_path)

    def open(self, selector: str) -> WorkspaceRecord:
        if not isinstance(selector, str) or not selector.strip():
            raise WorkspaceNotFoundError("workspace selector must not be empty")

        workspaces = self.workspaces_dir
        if not workspaces.exists():
            raise WorkspaceNotFoundError(f"workspace not found: {selector}")
        if workspaces.is_symlink() or not workspaces.is_dir():
            raise WorkspaceManagerError(f"workspaces path is not a real directory: {workspaces}")

        by_id = workspaces / selector
        if by_id.is_dir() and not by_id.is_symlink():
            return self._load_workspace(by_id)

        matches: list[WorkspaceRecord] = []
        for entry in sorted(workspaces.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            metadata_path = entry / "workspace.json"
            if not metadata_path.is_file():
                continue
            record = self._load_workspace(entry)
            if record.workspace_name == selector:
                matches.append(record)

        if not matches:
            raise WorkspaceNotFoundError(f"workspace not found: {selector}")
        if len(matches) > 1:
            raise WorkspaceManagerError(f"workspace name is ambiguous: {selector}")
        return matches[0]

    def reset(self, selector: str) -> WorkspaceRecord:
        with self._lifecycle_lock():
            with self.locked(selector) as current:
                target_dir = self.data_root / "targets" / current.target_lock_digest
                self._validate_target_binding(target_dir, current)
                report = extract_materialization_seed(
                    self.data_root,
                    target_dir,
                    podman_binary=self.podman_binary,
                )
                staging_root = Path(report.staging_path).resolve()
                staging_parent = staging_root.parent
                _write_json_exclusive(
                    staging_parent / "workspace.json",
                    _workspace_metadata(current, current.generation + 1),
                )
                _rename_exchange(staging_parent, current.workspace_path)
                try:
                    _remove_tree_in_namespace(
                        staging_parent,
                        podman_binary=self.podman_binary,
                    )
                except WorkspaceManagerError as exc:
                    raise WorkspaceManagerError(
                        f"workspace reset was published but old tree cleanup failed: {exc}"
                    ) from exc
                return self.open(current.workspace_id)

    def remove(self, selector: str) -> WorkspaceRecord:
        with self._lifecycle_lock():
            with self.locked(selector) as current:
                self._ensure_staging_dir()
                tombstone = self.staging_dir / (
                    f".workspace-remove-{current.workspace_id}-{uuid.uuid4().hex}"
                )
                try:
                    os.rename(current.workspace_path, tombstone)
                except OSError as exc:
                    raise WorkspaceManagerError(
                        f"cannot atomically remove workspace {current.workspace_id}: {exc}"
                    ) from exc
                try:
                    _remove_tree_in_namespace(
                        tombstone,
                        podman_binary=self.podman_binary,
                    )
                except WorkspaceManagerError as exc:
                    raise WorkspaceManagerError(
                        f"workspace was unpublished but tree cleanup failed: {exc}"
                    ) from exc
                return current

    def recover_staging(self) -> tuple[Path, ...]:
        """Delete abandoned workspace staging trees without touching base staging."""
        with self._lifecycle_lock():
            staging = self.staging_dir
            if not staging.exists():
                return ()
            if staging.is_symlink() or not staging.is_dir():
                raise WorkspaceManagerError(f"staging path is not a real directory: {staging}")
            removed: list[Path] = []
            for entry in sorted(staging.iterdir()):
                if not (
                    _WORKSPACE_STAGING.fullmatch(entry.name)
                    or _REMOVE_STAGING.fullmatch(entry.name)
                ):
                    continue
                if entry.is_symlink() or not entry.is_dir():
                    continue
                _remove_tree_in_namespace(entry, podman_binary=self.podman_binary)
                removed.append(entry)
            return tuple(removed)

    @contextmanager
    def locked(self, selector: str) -> Iterator[WorkspaceRecord]:
        record = self.open(selector)
        lock_path = self._locks_dir() / f"{record.workspace_id}.lock"
        with self._file_lock(lock_path):
            yield self.open(record.workspace_id)

    @contextmanager
    def _lifecycle_lock(self) -> Iterator[None]:
        with self._file_lock(self._locks_dir() / "lifecycle.lock"):
            yield

    @contextmanager
    def _file_lock(self, lock_path: Path) -> Iterator[None]:
        try:
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise WorkspaceManagerError(f"cannot lock workspace lifecycle: {exc}") from exc

    def _locks_dir(self) -> Path:
        locks_dir = self.data_root / "state" / "workspace-locks"
        if locks_dir.is_symlink() or (locks_dir.exists() and not locks_dir.is_dir()):
            raise WorkspaceManagerError(f"workspace lock path is not a real directory: {locks_dir}")
        locks_dir.mkdir(parents=True, mode=0o755, exist_ok=True)
        return locks_dir

    def _ensure_staging_dir(self) -> None:
        staging = self.staging_dir
        if staging.is_symlink() or (staging.exists() and not staging.is_dir()):
            raise WorkspaceManagerError(f"staging path is not a real directory: {staging}")
        staging.mkdir(mode=0o755, exist_ok=True)

    def _validate_target_binding(self, target_dir: Path, record: WorkspaceRecord) -> None:
        target = Path(target_dir).resolve()
        if target.parent != self.data_root / "targets" or not target.is_dir():
            raise WorkspaceManagerError(
                f"workspace target directory does not exist: {target}"
            )
        seed = _load_json_object(target / "materialization" / "seed.json")
        if seed.get("target_lock_digest") != record.target_lock_digest:
            raise WorkspaceManagerError("workspace target lock no longer matches materialization seed")
        if seed.get("base_digest") != record.base_digest:
            raise WorkspaceManagerError("workspace base no longer matches materialization seed")

    def _load_workspace(self, workspace_path: Path) -> WorkspaceRecord:
        path = Path(workspace_path).resolve()
        if path.parent != self.workspaces_dir or not path.is_dir() or path.is_symlink():
            raise WorkspaceManagerError(f"workspace path is invalid: {path}")

        metadata = _load_json_object(path / "workspace.json")
        if metadata.get("format_version") != WORKSPACE_FORMAT_VERSION:
            raise WorkspaceManagerError("workspace metadata has unsupported format_version")

        workspace_id = _required_string(metadata, "workspace_id")
        if workspace_id != path.name:
            raise WorkspaceManagerError("workspace directory does not match workspace_id")

        generation = metadata.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise WorkspaceManagerError("workspace metadata has invalid 'generation'")

        root_path = path / "root"
        if root_path.is_symlink() or not root_path.is_dir():
            raise WorkspaceManagerError(f"workspace root is not a real directory: {root_path}")

        return WorkspaceRecord(
            workspace_id=workspace_id,
            workspace_name=_required_string(metadata, "workspace_name"),
            target_lock_digest=_required_digest(metadata, "target_lock_digest"),
            base_digest=_required_digest(metadata, "base_digest"),
            generation=generation,
            workspace_path=path,
            root_path=root_path,
        )
