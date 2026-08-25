from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .workspace_manager import WorkspaceManager, WorkspaceManagerError


class StorageError(RuntimeError):
    """Base error for storage inspection and explicit deletion."""


class DeletionConfirmationRequired(StorageError):
    """Raised when a destructive operation was not explicitly confirmed."""


class DeletionBlockedError(StorageError):
    """Raised when a storage dependency prevents deletion."""


@dataclass(frozen=True, slots=True)
class StorageEntry:
    kind: str
    identifier: str
    label: str
    path: Path
    bytes_used: int


@dataclass(frozen=True, slots=True)
class StorageStatus:
    sdkm_cache_bytes: int
    base_bytes: int
    workspace_bytes: int
    build_output_bytes: int
    bases: tuple[StorageEntry, ...]
    workspaces: tuple[StorageEntry, ...]

    @property
    def tracked_bytes(self) -> int:
        return (
            self.sdkm_cache_bytes
            + self.base_bytes
            + self.workspace_bytes
            + self.build_output_bytes
        )


@dataclass(frozen=True, slots=True)
class DeletionPlan:
    kind: str
    identifier: str
    path: Path
    bytes_used: int
    blocked_by: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.blocked_by


def _allocated_tree_bytes(path: Path) -> int:
    """Return filesystem-allocated bytes without following symlinks.

    Hard-linked inodes are counted once inside one measured tree. Reflink/shared
    extent accounting remains filesystem-dependent; MVP does not build a custom
    block accounting layer.
    """

    root = Path(path)
    if not root.exists() and not root.is_symlink():
        return 0

    seen: set[tuple[int, int]] = set()

    def measure(current: Path) -> int:
        try:
            stat = current.lstat()
        except OSError as exc:
            raise StorageError(f"cannot inspect storage path {current}: {exc}") from exc

        identity = (stat.st_dev, stat.st_ino)
        if identity in seen:
            return 0
        seen.add(identity)
        blocks = getattr(stat, "st_blocks", 0)
        size = int(blocks) * 512 if blocks else int(stat.st_size)

        if current.is_symlink() or not current.is_dir():
            return size
        try:
            children = tuple(current.iterdir())
        except OSError as exc:
            raise StorageError(f"cannot list storage path {current}: {exc}") from exc
        return size + sum(measure(child) for child in children)

    return measure(root)


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"cannot read storage metadata {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StorageError(f"storage metadata is not a JSON object: {path}")
    return value


def _validate_digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise StorageError("target lock digest must be 64 lowercase hexadecimal characters")
    return value


def _remove_directory_contents(directory: Path) -> None:
    if not directory.exists():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise StorageError(f"storage path is not a real directory: {directory}")
    for entry in tuple(directory.iterdir()):
        try:
            if entry.is_symlink() or not entry.is_dir():
                entry.unlink()
            else:
                shutil.rmtree(entry)
        except OSError as exc:
            raise StorageError(f"cannot remove storage path {entry}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class StorageManager:
    data_root: Path
    podman_binary: str = "podman"

    def __post_init__(self) -> None:
        root = Path(self.data_root).expanduser().resolve()
        if not root.is_dir():
            raise StorageError(f"data root does not exist: {root}")
        object.__setattr__(self, "data_root", root)

    def status(self) -> StorageStatus:
        bases = self._base_entries()
        workspaces = self._workspace_entries()
        sdkm_cache = self.data_root / "sdkm" / "downloads"
        build_outputs = self.data_root / "build" / "outputs"
        return StorageStatus(
            sdkm_cache_bytes=_allocated_tree_bytes(sdkm_cache),
            base_bytes=sum(entry.bytes_used for entry in bases),
            workspace_bytes=sum(entry.bytes_used for entry in workspaces),
            build_output_bytes=_allocated_tree_bytes(build_outputs),
            bases=bases,
            workspaces=workspaces,
        )

    def plan_workspace_remove(self, selector: str) -> DeletionPlan:
        try:
            record = WorkspaceManager(
                self.data_root,
                podman_binary=self.podman_binary,
            ).open(selector)
        except WorkspaceManagerError as exc:
            raise StorageError(str(exc)) from exc
        return DeletionPlan(
            kind="workspace",
            identifier=record.workspace_id,
            path=record.workspace_path,
            bytes_used=_allocated_tree_bytes(record.workspace_path),
        )

    def remove_workspace(
        self,
        selector: str,
        *,
        confirmation: str | None = None,
    ) -> DeletionPlan:
        plan = self.plan_workspace_remove(selector)
        if confirmation != plan.identifier:
            raise DeletionConfirmationRequired(
                f"workspace deletion requires confirmation token: {plan.identifier}"
            )
        try:
            WorkspaceManager(
                self.data_root,
                podman_binary=self.podman_binary,
            ).remove(plan.identifier)
        except WorkspaceManagerError as exc:
            raise StorageError(str(exc)) from exc
        return plan

    def plan_base_remove(self, target_lock_digest: str) -> DeletionPlan:
        digest = _validate_digest(target_lock_digest)
        target = self.data_root / "targets" / digest
        if target.parent != self.data_root / "targets" or target.is_symlink() or not target.is_dir():
            raise StorageError(f"published base target not found: {digest}")
        dependencies = self._dependent_workspaces(digest)
        return DeletionPlan(
            kind="base",
            identifier=digest,
            path=target,
            bytes_used=_allocated_tree_bytes(target),
            blocked_by=dependencies,
        )

    def remove_base(
        self,
        target_lock_digest: str,
        *,
        confirmation: str | None = None,
    ) -> DeletionPlan:
        plan = self.plan_base_remove(target_lock_digest)
        if confirmation != plan.identifier:
            raise DeletionConfirmationRequired(
                f"base deletion requires confirmation token: {plan.identifier}"
            )
        with self._workspace_lifecycle_lock():
            plan = self.plan_base_remove(target_lock_digest)
            if plan.blocked_by:
                joined = ", ".join(plan.blocked_by)
                raise DeletionBlockedError(
                    f"base {plan.identifier} is still referenced by workspace(s): {joined}"
                )
            try:
                shutil.rmtree(plan.path)
            except OSError as exc:
                raise StorageError(f"cannot remove base {plan.identifier}: {exc}") from exc
            return plan

    def plan_sdkm_cache_remove(self) -> DeletionPlan:
        downloads = self.data_root / "sdkm" / "downloads"
        return DeletionPlan(
            kind="sdkm-cache",
            identifier="sdkm-downloads",
            path=downloads,
            bytes_used=_allocated_tree_bytes(downloads),
        )

    def remove_sdkm_cache(self, *, confirmation: str | None = None) -> DeletionPlan:
        plan = self.plan_sdkm_cache_remove()
        if confirmation != plan.identifier:
            raise DeletionConfirmationRequired(
                f"SDK Manager cache deletion requires confirmation token: {plan.identifier}"
            )
        with self._sdkm_acquisition_lock():
            # Re-plan after the lock because an acquisition may have completed while waiting.
            plan = self.plan_sdkm_cache_remove()
            _remove_directory_contents(plan.path)
        return plan

    def _base_entries(self) -> tuple[StorageEntry, ...]:
        targets = self.data_root / "targets"
        if not targets.exists():
            return ()
        if targets.is_symlink() or not targets.is_dir():
            raise StorageError(f"targets path is not a real directory: {targets}")
        entries: list[StorageEntry] = []
        for target in sorted(targets.iterdir()):
            if target.is_symlink() or not target.is_dir() or target.name.startswith("."):
                continue
            if len(target.name) != 64 or any(c not in "0123456789abcdef" for c in target.name):
                continue
            label = target.name
            lock = target / "lock.json"
            if lock.is_file():
                metadata = _load_json_object(lock)
                target_section = metadata.get("target")
                if isinstance(target_section, dict):
                    canonical_id = target_section.get("canonical_id")
                    if isinstance(canonical_id, str) and canonical_id:
                        label = canonical_id
            entries.append(
                StorageEntry(
                    kind="base",
                    identifier=target.name,
                    label=label,
                    path=target,
                    bytes_used=_allocated_tree_bytes(target),
                )
            )
        return tuple(entries)

    def _workspace_entries(self) -> tuple[StorageEntry, ...]:
        workspaces = self.data_root / "workspaces"
        if not workspaces.exists():
            return ()
        if workspaces.is_symlink() or not workspaces.is_dir():
            raise StorageError(f"workspaces path is not a real directory: {workspaces}")
        entries: list[StorageEntry] = []
        manager = WorkspaceManager(self.data_root, podman_binary=self.podman_binary)
        for workspace in sorted(workspaces.iterdir()):
            if workspace.is_symlink() or not workspace.is_dir():
                continue
            try:
                record = manager.open(workspace.name)
            except WorkspaceManagerError as exc:
                raise StorageError(str(exc)) from exc
            entries.append(
                StorageEntry(
                    kind="workspace",
                    identifier=record.workspace_id,
                    label=record.workspace_name,
                    path=record.workspace_path,
                    bytes_used=_allocated_tree_bytes(record.workspace_path),
                )
            )
        return tuple(entries)

    def _dependent_workspaces(self, target_lock_digest: str) -> tuple[str, ...]:
        workspaces = self.data_root / "workspaces"
        if not workspaces.exists():
            return ()
        if workspaces.is_symlink() or not workspaces.is_dir():
            raise StorageError(f"workspaces path is not a real directory: {workspaces}")
        result: list[str] = []
        for workspace in sorted(workspaces.iterdir()):
            if workspace.is_symlink():
                raise StorageError(
                    f"cannot prove base is unused while workspace path is a symlink: {workspace}"
                )
            if not workspace.is_dir():
                continue
            metadata_path = workspace / "workspace.json"
            if not metadata_path.is_file():
                raise StorageError(
                    f"cannot prove base is unused; workspace metadata is missing: {metadata_path}"
                )
            metadata = _load_json_object(metadata_path)
            if metadata.get("target_lock_digest") != target_lock_digest:
                continue
            identifier = metadata.get("workspace_id")
            name = metadata.get("workspace_name")
            if isinstance(name, str) and name:
                result.append(name)
            elif isinstance(identifier, str) and identifier:
                result.append(identifier)
        return tuple(result)

    @contextmanager
    def _workspace_lifecycle_lock(self) -> Iterator[None]:
        locks = self.data_root / "state" / "workspace-locks"
        if locks.is_symlink() or (locks.exists() and not locks.is_dir()):
            raise StorageError(f"workspace lock path is not a real directory: {locks}")
        locks.mkdir(parents=True, mode=0o755, exist_ok=True)
        with self._file_lock(locks / "lifecycle.lock"):
            yield

    @contextmanager
    def _sdkm_acquisition_lock(self) -> Iterator[None]:
        sdkm = self.data_root / "sdkm"
        if sdkm.is_symlink() or (sdkm.exists() and not sdkm.is_dir()):
            raise StorageError(f"SDK Manager path is not a real directory: {sdkm}")
        sdkm.mkdir(parents=True, mode=0o755, exist_ok=True)
        with self._file_lock(sdkm / ".acquisition.lock"):
            yield

    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[None]:
        try:
            with path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise StorageError(f"cannot lock storage operation: {exc}") from exc
