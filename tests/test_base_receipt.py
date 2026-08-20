from __future__ import annotations

import hashlib
from pathlib import Path

from orin_stage.acquisition.artifact_verification import VerifiedAcquisitionArtifact
from orin_stage.base._json import write_json_atomic
from orin_stage.base.identity import build_base_digest, build_base_target_projection_digest
from orin_stage.base.lock import target_lock_digest, write_target_lock
from orin_stage.base.packages import ConstructionPackageSet, LockedPackage, PackageSeed
from orin_stage.base.receipt import (
    base_directory_is_reusable,
    make_base_receipt,
    write_base_receipt,
)
from orin_stage.base.recipe import construction_recipe_digest_v1


def _artifact(kind: str, content: bytes) -> VerifiedAcquisitionArtifact:
    sha1 = hashlib.sha1(content).hexdigest()
    return VerifiedAcquisitionArtifact(
        kind=kind,
        filename=f"{kind}.tbz2",
        relative_path=f"{kind}.tbz2",
        size=len(content),
        sha1=sha1,
        sha256=hashlib.sha256(content).hexdigest(),
        official_sha1=sha1,
    )


def _package_set() -> ConstructionPackageSet:
    return ConstructionPackageSet(
        seed=PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64"),
        repository_base="https://repo.download.nvidia.com/jetson/",
        repository_suites=("common r36.5 main", "t234 r36.5 main"),
        packages=(
            LockedPackage("nvidia-jetpack", "6.2.3+b81", "arm64", "install", "jp.deb", "a" * 64),
        ),
    )


def _lock(artifacts: tuple[VerifiedAcquisitionArtifact, ...], packages: ConstructionPackageSet) -> dict[str, object]:
    by_kind = {item.kind: item for item in artifacts}
    return {
        "schema_version": 1,
        "target": {
            "canonical_id": "nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
            "jetpack_version": "6.2.3",
            "l4t_version": "36.5.2",
            "ubuntu_suite": "jammy",
            "target_abi": "aarch64",
            "debian_architecture": "arm64",
            "repository_platform": "t234",
        },
        "artifacts": {
            kind: {
                "filename": item.filename,
                "size": item.size,
                "official_sha1": item.official_sha1,
                "sha256": item.sha256,
            }
            for kind, item in by_kind.items()
        },
        "construction_packages": packages.to_dict(),
        "construction": {"recipe_digest": construction_recipe_digest_v1()},
    }


def test_published_base_metadata_is_reusable_without_rescanning_rootfs(tmp_path: Path) -> None:
    artifacts = (_artifact("bsp", b"bsp"), _artifact("sample_rootfs", b"rootfs"))
    packages = _package_set()
    lock = _lock(artifacts, packages)
    projection = build_base_target_projection_digest(lock)
    base_digest = build_base_digest(
        base_target_projection_digest=projection,
        construction_recipe_digest=construction_recipe_digest_v1(),
        artifacts=artifacts,
    )
    lock_digest = target_lock_digest(lock)
    target_dir = tmp_path / lock_digest
    (target_dir / "base").mkdir(parents=True)
    lock_path = target_dir / "lock.json"
    manifest_path = target_dir / "manifest.json"
    receipt_path = target_dir / "receipt.json"
    write_target_lock(lock_path, lock)
    write_json_atomic(manifest_path, {"schema_version": 1, "base_digest": base_digest})
    receipt = make_base_receipt(
        base_digest=base_digest,
        target_lock_digest_value=lock_digest,
        base_target_projection_digest=projection,
        construction_recipe_digest=construction_recipe_digest_v1(),
        construction_package_set_digest=packages.digest(),
        artifacts=artifacts,
        manifest_path=manifest_path,
    )
    write_base_receipt(receipt_path, receipt)

    assert base_directory_is_reusable(target_dir)

    manifest_path.write_text("tampered\n", encoding="utf-8")
    assert not base_directory_is_reusable(target_dir)
