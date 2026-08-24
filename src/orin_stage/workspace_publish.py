from __future__ import annotations

import ctypes
import errno
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .materialization_extract import (
    ExtractionReport,
    extract_materialization_seed,
    validate_materialization_staging,
)


WORKSPACE_FORMAT_VERSION = 1
RENAME_NOREPLACE = 1
AT_FDCWD = -100


class WorkspacePublishError(RuntimeError):
    """Raised when a validated staging tree cannot be published safely."""


@dataclass(frozen=True, slots=True)
class WorkspacePublishResult:
    workspace_path: Path
    workspace_id: str
    extraction_report: ExtractionReport
    reused_staging: bool


def _validate_workspace_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise WorkspacePublishError("workspace name must not be empty")
    if "/" in name or ".." in name:
        raise WorkspacePublishError("workspace name contains path traversal")
    return name


def _validate_workspace_id(workspace_id: str) -> str:
    if not workspace_id or "/" in workspace_id or ".." in workspace_id:
        raise WorkspacePublishError("generated workspace ID is unsafe")
    return workspace_id


def _load_json_object(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspacePublishError(f"cannot read metadata file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspacePublishError(f"metadata file is not a JSON object: {path}")
    return value


def _seed_identity(target_dir: Path) -> tuple[str, str]:
    metadata_path = target_dir / "materialization" / "seed.json"
    metadata = _load_json_object(metadata_path)
    values: list[str] = []
    for field in ("target_lock_digest", "base_digest"):
        value = metadata.get(field)
        if not isinstance(value, str) or not value:
            raise WorkspacePublishError(f"seed.json has invalid {field!r}")
        values.append(value)
    return values[0], values[1]


def _check_existing_workspaces(
    workspaces_dir: Path,
    workspace_id: str,
    workspace_name: str,
) -> None:
    destination = workspaces_dir / workspace_id
    if os.path.lexists(destination):
        raise WorkspacePublishError(
            f"workspace ID already exists: {workspace_id}"
        )
    if not workspaces_dir.exists():
        return
    if workspaces_dir.is_symlink() or not workspaces_dir.is_dir():
        raise WorkspacePublishError(
            f"workspaces path is not a real directory: {workspaces_dir}"
        )
    for entry in sorted(workspaces_dir.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        metadata_path = entry / "workspace.json"
        if not metadata_path.is_file():
            continue
        metadata = _load_json_object(metadata_path)
        if metadata.get("workspace_name") == workspace_name:
            raise WorkspacePublishError(
                f"workspace name already exists: {workspace_name}"
            )


def _validated_staging_root(data_root: Path, staging_path: Path) -> Path:
    requested = Path(staging_path).expanduser()
    if requested.is_symlink():
        raise WorkspacePublishError("staging root must not be a symlink")
    root = requested.resolve()
    expected_parent = data_root / "staging"
    if (
        root.name != "root"
        or root.parent.parent != expected_parent
        or not root.is_dir()
    ):
        raise WorkspacePublishError(
            f"staging root is not <data-root>/staging/<uuid>/root: {root}"
        )
    if root.parent.is_symlink():
        raise WorkspacePublishError("staging parent must not be a symlink")
    return root


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise WorkspacePublishError(
            "atomic no-replace publish requires Linux renameat2"
        ) from exc
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
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise WorkspacePublishError(
            f"workspace destination already exists: {destination}"
        )
    if error_number == errno.EXDEV:
        raise WorkspacePublishError(
            "staging and workspaces must be on the same filesystem"
        )
    raise WorkspacePublishError(
        f"atomic workspace publish failed: {os.strerror(error_number)}"
    )


Extractor = Callable[[Path, Path], ExtractionReport]
Validator = Callable[[Path, Path], ExtractionReport]


def _new_workspace_id() -> str:
    return uuid.uuid4().hex


def publish_materialization_workspace(
    data_root: Path,
    target_dir: Path,
    workspace_name: str,
    *,
    staging_root: Path | None = None,
    workspace_id_factory: Callable[[], str] = _new_workspace_id,
    extractor: Extractor = extract_materialization_seed,
    validator: Validator = validate_materialization_staging,
) -> WorkspacePublishResult:
    name = _validate_workspace_name(workspace_name)
    data = Path(data_root).expanduser().resolve()
    target = Path(target_dir).expanduser().resolve()
    if not data.is_dir():
        raise WorkspacePublishError(f"data root does not exist: {data}")
    if target.parent != data / "targets" or not target.is_dir():
        raise WorkspacePublishError(
            f"target directory is not under {data / 'targets'}: {target}"
        )
    target_lock_digest, base_digest = _seed_identity(target)
    workspace_id = _validate_workspace_id(workspace_id_factory())
    workspaces_dir = data / "workspaces"
    _check_existing_workspaces(workspaces_dir, workspace_id, name)

    if staging_root is None:
        report = extractor(data, target)
        root = _validated_staging_root(data, Path(report.staging_path))
        reused_staging = False
    else:
        root = _validated_staging_root(data, staging_root)
        report = validator(target / "materialization", root)
        if report.staging_path != str(root):
            raise WorkspacePublishError(
                "parity validator reported an unexpected staging path"
            )
        reused_staging = True

    staging_parent = root.parent
    metadata_path = staging_parent / "workspace.json"
    if os.path.lexists(metadata_path):
        raise WorkspacePublishError(
            f"refusing to overwrite staging metadata: {metadata_path}"
        )
    metadata = {
        "format_version": WORKSPACE_FORMAT_VERSION,
        "workspace_id": workspace_id,
        "workspace_name": name,
        "target_lock_digest": target_lock_digest,
        "base_digest": base_digest,
        "generation": 0,
    }
    try:
        with metadata_path.open("x", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise WorkspacePublishError(
            f"cannot write staging workspace metadata: {exc}"
        ) from exc

    if workspaces_dir.is_symlink():
        raise WorkspacePublishError(
            f"workspaces path is not a real directory: {workspaces_dir}"
        )
    workspaces_dir.mkdir(mode=0o755, exist_ok=True)
    destination = workspaces_dir / workspace_id
    _rename_noreplace(staging_parent, destination)
    return WorkspacePublishResult(
        workspace_path=destination,
        workspace_id=workspace_id,
        extraction_report=report,
        reused_staging=reused_staging,
    )
