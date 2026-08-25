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
from typing import Callable, Iterator, Mapping, Sequence

from .build_capsule import BuildCapsuleRunner
from .materialization_extract import extract_materialization_seed
from .target_executor import TargetExecutor
from .workspace_publish import (
    WORKSPACE_FORMAT_VERSION,
    WorkspacePublishError,
    publish_materialization_workspace,
)


AT_FDCWD = -100
RENAME_EXCHANGE = 2
OPERATION_FORMAT_VERSION = 1
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


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise WorkspaceManagerError(f"cannot atomically write JSON {path}: {exc}") from exc


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

    @property
    def operations_dir(self) -> Path:
        return self.data_root / "state" / "operations"

    def create(self, target_dir: Path, workspace_name: str) -> WorkspaceRecord:
        with self._lifecycle_lock():
            target_lock_digest, base_digest = self._target_identity(target_dir)
            operation_id, receipt = self._start_operation(
                "create",
                workspace_name=workspace_name,
                target_lock_digest=target_lock_digest,
                base_digest=base_digest,
                generation_after=0,
            )
            published = False
            try:
                result = publish_materialization_workspace(
                    self.data_root,
                    target_dir,
                    workspace_name,
                )
                published = True
                record = self._load_workspace(result.workspace_path)
                self._update_operation(
                    operation_id,
                    receipt,
                    phase="completed",
                    workspace_id=record.workspace_id,
                    final_path=str(record.workspace_path),
                )
                return record
            except Exception as exc:
                if not published:
                    self._update_operation(
                        operation_id,
                        receipt,
                        phase="failed",
                        error=str(exc),
                    )
                elif isinstance(exc, WorkspaceManagerError):
                    raise WorkspaceManagerError(
                        f"workspace create was published but receipt/metadata finalization failed: {exc}"
                    ) from exc
                if isinstance(exc, WorkspacePublishError):
                    raise WorkspaceManagerError(str(exc)) from exc
                raise

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

    def run(
        self,
        selector: str,
        command: Sequence[str],
        *,
        executor: TargetExecutor | None = None,
        runner: Runner = subprocess.run,
    ) -> subprocess.CompletedProcess[str]:
        """Run one mutable target command while holding the workspace generation lock."""
        selected = executor or TargetExecutor(podman_binary=self.podman_binary)
        with self.locked(selector) as current:
            completed = selected.run(current.root_path, command, runner=runner)
            self._advance_generation(current)
            return completed

    def shell(
        self,
        selector: str,
        *,
        executor: TargetExecutor | None = None,
        runner: Runner = subprocess.run,
    ) -> subprocess.CompletedProcess[str]:
        """Open one mutable target shell and advance generation after a clean exit."""
        selected = executor or TargetExecutor(podman_binary=self.podman_binary)
        with self.locked(selector) as current:
            completed = selected.shell(current.root_path, runner=runner)
            self._advance_generation(current)
            return completed

    def build(
        self,
        selector: str,
        repository_root: Path,
        toolchain_root: Path,
        command: Sequence[str],
        *,
        build_runner: BuildCapsuleRunner | None = None,
        runner: Runner = subprocess.run,
    ) -> subprocess.CompletedProcess[str]:
        """Build against one locked workspace generation without mutating it."""
        selected = build_runner or BuildCapsuleRunner(podman_binary=self.podman_binary)
        with self.locked(selector) as current:
            return selected.run(
                current.root_path,
                repository_root,
                toolchain_root,
                command,
                runner=runner,
            )

    def reset(self, selector: str) -> WorkspaceRecord:
        with self._lifecycle_lock():
            with self.locked(selector) as current:
                target_dir = self.data_root / "targets" / current.target_lock_digest
                self._validate_target_binding(target_dir, current)
                operation_id, receipt = self._start_operation(
                    "reset",
                    workspace_id=current.workspace_id,
                    workspace_name=current.workspace_name,
                    target_lock_digest=current.target_lock_digest,
                    base_digest=current.base_digest,
                    generation_before=current.generation,
                    generation_after=current.generation + 1,
                    final_path=str(current.workspace_path),
                )
                published = False
                try:
                    report = extract_materialization_seed(
                        self.data_root,
                        target_dir,
                        podman_binary=self.podman_binary,
                    )
                    staging_root = Path(report.staging_path).resolve()
                    staging_parent = staging_root.parent
                    self._update_operation(
                        operation_id,
                        receipt,
                        phase="staged",
                        staging_path=str(staging_parent),
                    )
                    _write_json_exclusive(
                        staging_parent / "workspace.json",
                        _workspace_metadata(current, current.generation + 1),
                    )
                    _rename_exchange(staging_parent, current.workspace_path)
                    published = True
                    self._update_operation(
                        operation_id,
                        receipt,
                        phase="published",
                        staging_path=str(staging_parent),
                    )
                    _remove_tree_in_namespace(
                        staging_parent,
                        podman_binary=self.podman_binary,
                    )
                    self._update_operation(
                        operation_id,
                        receipt,
                        phase="completed",
                        staging_path=str(staging_parent),
                    )
                    return self.open(current.workspace_id)
                except Exception as exc:
                    if not published:
                        self._update_operation(
                            operation_id,
                            receipt,
                            phase="failed",
                            error=str(exc),
                        )
                    elif isinstance(exc, WorkspaceManagerError):
                        raise WorkspaceManagerError(
                            f"workspace reset was published but cleanup/receipt failed: {exc}"
                        ) from exc
                    raise

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
        """Delete abandoned workspace staging trees and reconcile operation receipts."""
        with self._lifecycle_lock():
            staging = self.staging_dir
            removed: list[Path] = []
            if staging.exists():
                if staging.is_symlink() or not staging.is_dir():
                    raise WorkspaceManagerError(f"staging path is not a real directory: {staging}")
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
            self._recover_operations()
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

    def _target_identity(self, target_dir: Path) -> tuple[str, str]:
        target = Path(target_dir).expanduser().resolve()
        if target.parent != self.data_root / "targets" or not target.is_dir():
            raise WorkspaceManagerError(
                f"workspace target directory does not exist: {target}"
            )
        seed = _load_json_object(target / "materialization" / "seed.json")
        return (
            _required_digest(seed, "target_lock_digest"),
            _required_digest(seed, "base_digest"),
        )

    def _validate_target_binding(self, target_dir: Path, record: WorkspaceRecord) -> None:
        target_lock_digest, base_digest = self._target_identity(target_dir)
        if target_lock_digest != record.target_lock_digest:
            raise WorkspaceManagerError("workspace target lock no longer matches materialization seed")
        if base_digest != record.base_digest:
            raise WorkspaceManagerError("workspace base no longer matches materialization seed")

    def _advance_generation(self, current: WorkspaceRecord) -> WorkspaceRecord:
        latest = self.open(current.workspace_id)
        if latest.generation != current.generation:
            raise WorkspaceManagerError("workspace generation changed while operation was locked")
        _write_json_atomic(
            current.workspace_path / "workspace.json",
            _workspace_metadata(current, current.generation + 1),
        )
        return self.open(current.workspace_id)

    def _start_operation(self, operation: str, **fields: object) -> tuple[str, dict[str, object]]:
        operation_id = uuid.uuid4().hex
        receipt: dict[str, object] = {
            "format_version": OPERATION_FORMAT_VERSION,
            "operation_id": operation_id,
            "operation": operation,
            "phase": "started",
            **fields,
        }
        self._write_operation(operation_id, receipt)
        return operation_id, receipt

    def _update_operation(
        self,
        operation_id: str,
        receipt: dict[str, object],
        *,
        phase: str,
        **fields: object,
    ) -> None:
        receipt.update(fields)
        receipt["phase"] = phase
        self._write_operation(operation_id, receipt)

    def _write_operation(self, operation_id: str, receipt: Mapping[str, object]) -> None:
        operations = self.operations_dir
        if operations.is_symlink() or (operations.exists() and not operations.is_dir()):
            raise WorkspaceManagerError(f"operations path is not a real directory: {operations}")
        _write_json_atomic(operations / f"{operation_id}.json", receipt)

    def _recover_operations(self) -> None:
        operations = self.operations_dir
        if not operations.exists():
            return
        if operations.is_symlink() or not operations.is_dir():
            raise WorkspaceManagerError(f"operations path is not a real directory: {operations}")

        for path in sorted(operations.glob("*.json")):
            receipt = dict(_load_json_object(path))
            if receipt.get("format_version") != OPERATION_FORMAT_VERSION:
                raise WorkspaceManagerError(f"unsupported operation receipt: {path}")
            phase = receipt.get("phase")
            if phase in {"completed", "failed"}:
                continue
            operation = receipt.get("operation")
            if operation == "create":
                self._recover_create_receipt(path.stem, receipt)
            elif operation == "reset":
                self._recover_reset_receipt(path.stem, receipt)

    def _recover_create_receipt(self, operation_id: str, receipt: dict[str, object]) -> None:
        name = _required_string(receipt, "workspace_name")
        target_lock_digest = _required_digest(receipt, "target_lock_digest")
        base_digest = _required_digest(receipt, "base_digest")
        matches: list[WorkspaceRecord] = []
        if self.workspaces_dir.is_dir() and not self.workspaces_dir.is_symlink():
            for entry in sorted(self.workspaces_dir.iterdir()):
                if entry.is_symlink() or not entry.is_dir():
                    continue
                try:
                    record = self._load_workspace(entry)
                except WorkspaceManagerError:
                    continue
                if (
                    record.workspace_name == name
                    and record.target_lock_digest == target_lock_digest
                    and record.base_digest == base_digest
                ):
                    matches.append(record)
        if len(matches) > 1:
            raise WorkspaceManagerError(f"cannot recover ambiguous create operation {operation_id}")
        if matches:
            record = matches[0]
            self._update_operation(
                operation_id,
                receipt,
                phase="completed",
                recovered=True,
                workspace_id=record.workspace_id,
                final_path=str(record.workspace_path),
            )
        else:
            self._update_operation(
                operation_id,
                receipt,
                phase="failed",
                recovered=True,
                error="create operation did not publish a workspace",
            )

    def _recover_reset_receipt(self, operation_id: str, receipt: dict[str, object]) -> None:
        workspace_id = _required_string(receipt, "workspace_id")
        before = receipt.get("generation_before")
        after = receipt.get("generation_after")
        if (
            isinstance(before, bool)
            or not isinstance(before, int)
            or before < 0
            or isinstance(after, bool)
            or not isinstance(after, int)
            or after != before + 1
        ):
            raise WorkspaceManagerError(f"reset operation has invalid generations: {operation_id}")
        try:
            record = self.open(workspace_id)
        except WorkspaceNotFoundError as exc:
            raise WorkspaceManagerError(
                f"reset recovery cannot find final workspace {workspace_id}"
            ) from exc
        if record.generation == after:
            self._update_operation(
                operation_id,
                receipt,
                phase="completed",
                recovered=True,
            )
        elif record.generation == before:
            self._update_operation(
                operation_id,
                receipt,
                phase="failed",
                recovered=True,
                error="reset operation did not publish the staged generation",
            )
        else:
            raise WorkspaceManagerError(
                f"reset recovery found unexpected generation {record.generation} for {workspace_id}"
            )

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
