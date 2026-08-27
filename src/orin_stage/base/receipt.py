from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from orin_stage.acquisition.artifact_verification import VerifiedAcquisitionArtifact

from ._json import file_sha256, json_digest, load_json_object, write_json_atomic
from .identity import (
    BASE_IDENTITY_POLICY_ID,
    BASE_IDENTITY_POLICY_VERSION,
    build_base_digest,
    build_base_target_projection_digest,
)
from .lock import load_target_lock, target_lock_digest
from .packages import PackageTransactionEvidence
from .validation import BASE_VALIDATION_POLICY_ID, BASE_VALIDATION_POLICY_VERSION


BASE_RECEIPT_SCHEMA_VERSION = 1


class BaseReceiptError(RuntimeError):
    """Raised when immutable-base receipt evidence is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class BaseReceipt:
    schema_version: int
    created_at: str
    base_digest: str
    target_lock_digest: str
    base_identity_policy_id: str
    base_identity_policy_version: int
    base_target_projection_digest: str
    construction_recipe_digest: str
    construction_package_set_digest: str
    packages_removed: tuple[str, ...]
    removal_policy_version: str
    allowed_removal_set: tuple[str, ...]
    construction_artifacts: tuple[dict[str, str], ...]
    manifest_sha256: str
    validation_policy_id: str
    validation_policy_version: int
    validation: str

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["construction_artifacts"] = list(self.construction_artifacts)
        data["packages_removed"] = list(self.packages_removed)
        data["allowed_removal_set"] = list(self.allowed_removal_set)
        return data


def make_base_receipt(
    *,
    base_digest: str,
    target_lock_digest_value: str,
    base_target_projection_digest: str,
    construction_recipe_digest: str,
    construction_package_set_digest: str,
    package_transaction: PackageTransactionEvidence,
    artifacts: tuple[VerifiedAcquisitionArtifact, ...],
    manifest_path: Path,
    now: Callable[[], datetime] | None = None,
) -> BaseReceipt:
    clock = now or (lambda: datetime.now(timezone.utc))
    created = clock()
    if created.tzinfo is None:
        raise BaseReceiptError("base receipt timestamp must be timezone-aware")

    artifact_rows = tuple(
        {"kind": artifact.kind, "sha256": artifact.sha256}
        for artifact in sorted(artifacts, key=lambda item: item.kind)
    )
    return BaseReceipt(
        schema_version=BASE_RECEIPT_SCHEMA_VERSION,
        created_at=created.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        base_digest=base_digest,
        target_lock_digest=target_lock_digest_value,
        base_identity_policy_id=BASE_IDENTITY_POLICY_ID,
        base_identity_policy_version=BASE_IDENTITY_POLICY_VERSION,
        base_target_projection_digest=base_target_projection_digest,
        construction_recipe_digest=construction_recipe_digest,
        construction_package_set_digest=construction_package_set_digest,
        packages_removed=package_transaction.packages_removed,
        removal_policy_version=package_transaction.removal_policy_version,
        allowed_removal_set=package_transaction.allowed_removal_set,
        construction_artifacts=artifact_rows,
        manifest_sha256=file_sha256(manifest_path),
        validation_policy_id=BASE_VALIDATION_POLICY_ID,
        validation_policy_version=BASE_VALIDATION_POLICY_VERSION,
        validation="passed",
    )


def write_base_receipt(
    path: Path,
    receipt: BaseReceipt,
    *,
    mode: int | None = None,
) -> None:
    write_json_atomic(path, receipt.to_dict(), mode=mode)


def load_base_receipt(path: Path) -> Mapping[str, object]:
    return load_json_object(path, expected_schema_version=BASE_RECEIPT_SCHEMA_VERSION)


def _artifact_objects_from_lock(lock: Mapping[str, object]) -> tuple[VerifiedAcquisitionArtifact, ...]:
    artifacts = lock.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise BaseReceiptError("target lock artifact section is malformed")
    result: list[VerifiedAcquisitionArtifact] = []
    for kind in ("bsp", "sample_rootfs"):
        item = artifacts.get(kind)
        if not isinstance(item, Mapping):
            raise BaseReceiptError(f"target lock is missing artifact {kind!r}")
        result.append(
            VerifiedAcquisitionArtifact(
                kind=kind,
                filename=str(item["filename"]),
                relative_path="",
                size=int(item["size"]),
                sha1=str(item["official_sha1"]),
                sha256=str(item["sha256"]),
                official_sha1=str(item["official_sha1"]),
            )
        )
    return tuple(result)


def base_directory_is_reusable(target_directory: Path) -> bool:
    """Cheap reuse gate: verify published metadata identity, not the whole rootfs tree."""

    directory = Path(target_directory)
    lock_path = directory / "lock.json"
    manifest_path = directory / "manifest.json"
    receipt_path = directory / "receipt.json"
    base_path = directory / "base"
    if not base_path.is_dir() or not manifest_path.is_file():
        return False

    try:
        lock = load_target_lock(lock_path)
        receipt = load_base_receipt(receipt_path)
        lock_digest = target_lock_digest(lock)
        if directory.name != lock_digest:
            return False
        projection_digest = build_base_target_projection_digest(lock)
        construction = lock.get("construction")
        packages = lock.get("construction_packages")
        if not isinstance(construction, Mapping) or not isinstance(packages, Mapping):
            return False
        package_removal = packages.get("package_removal")
        if not isinstance(package_removal, Mapping):
            return False
        recipe_digest = str(construction["recipe_digest"])
        expected_base = build_base_digest(
            base_target_projection_digest=projection_digest,
            construction_recipe_digest=recipe_digest,
            artifacts=_artifact_objects_from_lock(lock),
        )
        if receipt.get("base_digest") != expected_base:
            return False
        if receipt.get("target_lock_digest") != lock_digest:
            return False
        if receipt.get("base_identity_policy_id") != BASE_IDENTITY_POLICY_ID:
            return False
        if receipt.get("base_identity_policy_version") != BASE_IDENTITY_POLICY_VERSION:
            return False
        if receipt.get("base_target_projection_digest") != projection_digest:
            return False
        if receipt.get("construction_recipe_digest") != recipe_digest:
            return False
        if receipt.get("construction_package_set_digest") != json_digest(packages):
            return False
        if receipt.get("packages_removed") != package_removal.get("packages_removed"):
            return False
        if receipt.get("removal_policy_version") != package_removal.get(
            "removal_policy_version"
        ):
            return False
        if receipt.get("allowed_removal_set") != package_removal.get(
            "allowed_removal_set"
        ):
            return False
        if receipt.get("validation_policy_id") != BASE_VALIDATION_POLICY_ID:
            return False
        if receipt.get("validation_policy_version") != BASE_VALIDATION_POLICY_VERSION:
            return False
        if receipt.get("validation") != "passed":
            return False
        if receipt.get("manifest_sha256") != file_sha256(manifest_path):
            return False
        manifest = load_json_object(manifest_path, expected_schema_version=1)
        if manifest.get("base_digest") != expected_base:
            return False
    except (KeyError, TypeError, ValueError, OSError, BaseReceiptError, RuntimeError):
        return False
    return True
