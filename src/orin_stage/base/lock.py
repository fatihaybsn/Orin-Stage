from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from orin_stage.acquisition.artifact_verification import VerifiedAcquisitionArtifact
from orin_stage.catalog.resolver import ResolvedCatalogTarget

from ._json import file_sha256, json_digest, load_json_object, write_json_atomic
from .packages import ConstructionPackageSet
from .recipe import CONSTRUCTION_RECIPE_ID, CONSTRUCTION_RECIPE_VERSION


TARGET_LOCK_SCHEMA_VERSION = 1


class TargetLockError(RuntimeError):
    """Raised when a canonical target lock cannot be constructed or validated."""


def _artifact_map_from_receipt(
    receipt: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        raise TargetLockError("acquisition receipt has no artifact list")
    result: dict[str, Mapping[str, object]] = {}
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise TargetLockError("acquisition artifact entry is malformed")
        kind = item.get("kind")
        if not isinstance(kind, str) or kind in result:
            raise TargetLockError("acquisition artifact kind is missing or duplicated")
        result[kind] = item
    return result


def acquisition_artifacts_from_receipt(
    receipt: Mapping[str, object],
) -> tuple[VerifiedAcquisitionArtifact, ...]:
    result: list[VerifiedAcquisitionArtifact] = []
    for kind, item in sorted(_artifact_map_from_receipt(receipt).items()):
        try:
            result.append(
                VerifiedAcquisitionArtifact(
                    kind=kind,
                    filename=str(item["filename"]),
                    relative_path=str(item["relative_path"]),
                    size=int(item["size"]),
                    sha1=str(item["sha1"]),
                    sha256=str(item["sha256"]),
                    official_sha1=str(item["official_sha1"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TargetLockError(f"malformed acquisition artifact {kind!r}") from exc
    return tuple(result)


def build_canonical_target_lock(
    target: ResolvedCatalogTarget,
    *,
    acquisition_receipt: Mapping[str, object],
    acquisition_receipt_path: Path,
    data_root: Path,
    package_set: ConstructionPackageSet,
    construction_recipe_digest: str,
    qemu_version: str,
    adapter_version: int = 1,
) -> dict[str, object]:
    """Build the single canonical runtime lock for one realized JP6 target."""

    if acquisition_receipt.get("canonical_id") != target.canonical_id:
        raise TargetLockError("acquisition receipt canonical target does not match resolver target")
    if acquisition_receipt.get("jetpack_version") != target.record["release"]["jetpack"]["version"]:
        raise TargetLockError("acquisition receipt JetPack version does not match catalog target")

    root = Path(data_root).resolve()
    receipt_path = Path(acquisition_receipt_path).resolve()
    try:
        receipt_relative = str(receipt_path.relative_to(root))
    except ValueError as exc:
        raise TargetLockError("acquisition receipt must live below ORIN_STAGE_DATA") from exc

    receipt_artifacts = _artifact_map_from_receipt(acquisition_receipt)
    artifacts: dict[str, object] = {}
    for kind in ("bsp", "sample_rootfs"):
        item = receipt_artifacts.get(kind)
        if item is None:
            raise TargetLockError(f"acquisition receipt is missing {kind!r}")
        artifacts[kind] = {
            "filename": str(item["filename"]),
            "size": int(item["size"]),
            "official_sha1": str(item["official_sha1"]),
            "sha256": str(item["sha256"]),
        }

    record = target.record
    release = record["release"]
    userspace = record["userspace"]
    architecture = userspace["architecture"]
    repository = record["packages"]["repository"]
    cross_compiler = record["toolchain"]["cross_compiler"]

    lock: dict[str, object] = {
        "schema_version": TARGET_LOCK_SCHEMA_VERSION,
        "target": {
            "canonical_id": target.canonical_id,
            "jetpack_version": str(release["jetpack"]["version"]),
            "l4t_version": str(release["l4t"]["version"]),
            "jetson_linux_release_revision": str(
                release["jetson_linux"]["release_revision"]
            ),
            "ubuntu_version": str(userspace["ubuntu"]["version"]),
            "ubuntu_suite": str(userspace["ubuntu"]["suite"]),
            "target_abi": str(architecture["target_abi"]),
            "debian_architecture": str(architecture["debian_architecture"]),
            "repository_platform": str(record["target"]["soc"]["repository_platform"]),
        },
        "artifacts": artifacts,
        "construction_packages": package_set.to_dict(),
        "construction": {
            "adapter_id": "jp6-base-builder",
            "adapter_version": adapter_version,
            "recipe_id": CONSTRUCTION_RECIPE_ID,
            "recipe_version": CONSTRUCTION_RECIPE_VERSION,
            "recipe_digest": construction_recipe_digest,
            "qemu": {
                "implementation": "qemu-aarch64-static",
                "version": qemu_version,
            },
        },
        "acquisition": {
            "acquisition_digest": str(acquisition_receipt["acquisition_digest"]),
            "sdk_manager_version": str(acquisition_receipt["sdk_manager_version"]),
            "sdk_manager_query_source": str(
                acquisition_receipt["sdk_manager_query_source"]
            ),
            "sdk_manager_target": str(acquisition_receipt["sdk_manager_target"]),
            "role_id": str(acquisition_receipt["role_id"]),
            "role_digest": str(acquisition_receipt["role_digest"]),
            "response_file_sha256": str(
                acquisition_receipt["response_file_sha256"]
            ),
            "receipt_path": receipt_relative,
            "receipt_sha256": file_sha256(receipt_path),
        },
        "validation": {
            "policy_id": "base-validation-v1",
            "policy_version": 1,
        },
        "declared_environment": {
            "nvidia_stack": record["nvidia_stack"],
            "hardware_families": record["target"]["families"],
            "hardware_profiles": record["target"]["hardware_profiles"],
            "repository": {
                "base": str(repository["base"]),
                "suites": list(repository["suites"]),
                "channel_is_mutable": bool(repository["channel_is_mutable"]),
            },
            "cross_compiler": cross_compiler,
            "exact_cross_packages": {
                "status": "deferred-to-step6-build-capsule",
            },
        },
        "licensing": record["licensing"],
    }
    return lock


def target_lock_digest(lock: Mapping[str, Any]) -> str:
    if lock.get("schema_version") != TARGET_LOCK_SCHEMA_VERSION:
        raise TargetLockError("unsupported canonical target lock schema")
    return json_digest(lock)


def write_target_lock(path: Path, lock: Mapping[str, Any]) -> None:
    target_lock_digest(lock)
    write_json_atomic(path, lock)


def load_target_lock(path: Path) -> Mapping[str, object]:
    data = load_json_object(path, expected_schema_version=TARGET_LOCK_SCHEMA_VERSION)
    target_lock_digest(data)
    return data
