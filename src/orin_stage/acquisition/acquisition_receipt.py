from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .artifact_verification import (
    CONSTRUCTION_ARTIFACT_KINDS,
    VerifiedAcquisitionArtifact,
    verify_receipt_artifact,
)
from .sdk_manager_discovery import SdkManagerDiscovery
from .sdk_manager_response import SdkManagerResponseFile
from .sdk_manager_role import SdkManagerComponentRole


class AcquisitionReceiptError(RuntimeError):
    """Raised for malformed or non-matching acquisition receipts."""


@dataclass(frozen=True, slots=True)
class AcquisitionMetadataFile:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AcquisitionReceipt:
    schema_version: int
    acquisition_digest: str
    created_at: str
    canonical_id: str
    sdk_manager_version: str
    sdk_manager_query_source: str
    jetpack_version: str
    sdk_manager_target: str
    role_id: str
    role_digest: str
    include_host: bool
    selected_groups: tuple[str, ...]
    deselected_groups: tuple[str, ...]
    response_file_sha256: str
    download_root: str
    artifacts: tuple[VerifiedAcquisitionArtifact, ...]
    sdk_manager_metadata: tuple[AcquisitionMetadataFile, ...]
    license_handling: str

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["selected_groups"] = list(self.selected_groups)
        data["deselected_groups"] = list(self.deselected_groups)
        data["artifacts"] = [asdict(item) for item in self.artifacts]
        data["sdk_manager_metadata"] = [
            asdict(item) for item in self.sdk_manager_metadata
        ]
        return data


def build_acquisition_digest(
    discovery: SdkManagerDiscovery,
    response_file: SdkManagerResponseFile,
    role: SdkManagerComponentRole,
) -> str:
    """Build the identity of one SDK Manager acquisition transaction.

    This digest is intentionally acquisition-specific and must not be reused as
    immutable-base identity. In particular it includes SDK Manager and response
    file evidence that can change without changing the verified construction
    bytes. Base identity is built separately by ``orin_stage.base``.
    """

    payload = {
        "canonical_id": discovery.target.canonical_id,
        "sdk_manager_version": discovery.sdk_manager_version,
        "query_source": discovery.query_source,
        "jetpack_version": discovery.target.jetpack_version,
        "sdk_manager_target": discovery.target.sdk_manager_target,
        "role_id": role.role_id,
        "role_digest": role.digest(),
        "response_file_sha256": response_file.sha256,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def hash_metadata_files(
    directory: Path,
) -> tuple[AcquisitionMetadataFile, ...]:
    directory = Path(directory)
    if not directory.is_dir():
        return ()

    results: list[AcquisitionMetadataFile] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        results.append(
            AcquisitionMetadataFile(
                relative_path=str(path.relative_to(directory)),
                size=size,
                sha256=digest.hexdigest(),
            )
        )
    return tuple(results)


def make_receipt(
    discovery: SdkManagerDiscovery,
    role: SdkManagerComponentRole,
    response_file: SdkManagerResponseFile,
    *,
    download_root: Path,
    artifacts: tuple[VerifiedAcquisitionArtifact, ...],
    sdk_manager_metadata: tuple[AcquisitionMetadataFile, ...],
    now: Callable[[], datetime] | None = None,
) -> AcquisitionReceipt:
    clock = now or (lambda: datetime.now(timezone.utc))
    created = clock()
    if created.tzinfo is None:
        raise AcquisitionReceiptError("receipt timestamp must be timezone-aware")

    return AcquisitionReceipt(
        schema_version=1,
        acquisition_digest=build_acquisition_digest(discovery, response_file, role),
        created_at=created.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        canonical_id=discovery.target.canonical_id,
        sdk_manager_version=discovery.sdk_manager_version,
        sdk_manager_query_source=discovery.query_source,
        jetpack_version=discovery.target.jetpack_version,
        sdk_manager_target=discovery.target.sdk_manager_target,
        role_id=role.role_id,
        role_digest=role.digest(),
        include_host=role.include_host,
        selected_groups=role.select_groups,
        deselected_groups=role.deselect_groups,
        response_file_sha256=response_file.sha256,
        download_root=str(Path(download_root)),
        artifacts=artifacts,
        sdk_manager_metadata=sdk_manager_metadata,
        license_handling="sdk_manager_user_interaction",
    )


def write_receipt_atomic(path: Path, receipt: AcquisitionReceipt) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n"

    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_receipt(path: Path) -> Mapping[str, object]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise AcquisitionReceiptError(f"cannot load acquisition receipt: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise AcquisitionReceiptError("unsupported or malformed acquisition receipt")
    return data


def receipt_is_cache_hit(
    path: Path,
    *,
    expected_digest: str,
    download_root: Path,
) -> bool:
    """Validate the receipt identity plus every artifact/metadata hash."""

    try:
        data = load_receipt(path)
    except AcquisitionReceiptError:
        return False
    if data.get("acquisition_digest") != expected_digest:
        return False
    if data.get("download_root") != str(Path(download_root)):
        return False

    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        return False

    expected_kinds = frozenset(CONSTRUCTION_ARTIFACT_KINDS)
    seen_kinds: set[str] = set()
    if len(artifacts) != len(expected_kinds):
        return False

    for item in artifacts:
        if not isinstance(item, dict):
            return False
        kind = item.get("kind")
        if not isinstance(kind, str) or kind in seen_kinds:
            return False
        seen_kinds.add(kind)
        try:
            ok = verify_receipt_artifact(
                download_root,
                relative_path=str(item["relative_path"]),
                expected_size=int(item["size"]),
                expected_sha256=str(item["sha256"]),
            )
        except (KeyError, TypeError, ValueError):
            return False
        if not ok:
            return False

    if seen_kinds != expected_kinds:
        return False

    metadata = data.get("sdk_manager_metadata")
    if not isinstance(metadata, list) or not metadata:
        return False
    metadata_root = Path(path).parent / "metadata"
    for item in metadata:
        if not isinstance(item, dict):
            return False
        try:
            ok = verify_receipt_artifact(
                metadata_root,
                relative_path=str(item["relative_path"]),
                expected_size=int(item["size"]),
                expected_sha256=str(item["sha256"]),
            )
        except (KeyError, TypeError, ValueError):
            return False
        if not ok:
            return False
    return True
