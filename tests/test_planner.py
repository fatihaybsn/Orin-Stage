from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from dataclasses import replace
from pathlib import Path

from orin_stage.acquisition.acquisition_receipt import (
    AcquisitionMetadataFile,
    make_receipt,
    write_receipt_atomic,
)
from orin_stage.acquisition.artifact_verification import VerifiedAcquisitionArtifact
from orin_stage.acquisition.sdk_manager_discovery import SdkManagerDiscovery
from orin_stage.acquisition.sdk_manager_match import VerifiedSdkManagerTarget
from orin_stage.acquisition.sdk_manager_response import SdkManagerResponseFile
from orin_stage.acquisition.sdk_manager_role import JP6_DEVELOPER_ROLE_V1
from orin_stage.base._json import write_json_atomic
from orin_stage.base.identity import build_base_digest, build_base_target_projection_digest
from orin_stage.base.lock import target_lock_digest, write_target_lock
from orin_stage.base.packages import ConstructionPackageSet, LockedPackage, PackageSeed
from orin_stage.base.receipt import make_base_receipt, write_base_receipt
from orin_stage.base.recipe import construction_recipe_digest_v1
from orin_stage.catalog.resolver import TargetResolver
from orin_stage.planning import (
    ArtifactIndex,
    BasePlanStatus,
    PlanArtifactStatus,
    plan_release,
    rebuild_artifact_index,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_16GB = "orin-nx-16gb-p3767-0000-on-p3768-0000"
PROFILE_8GB = "orin-nx-8gb-p3767-0001-on-p3768-0000"


def _target(*, supported: bool = True):
    resolver = TargetResolver(
        REPO_ROOT / "catalog" / "targets",
        REPO_ROOT / "catalog" / "schema" / "target.schema.json",
    )
    target = resolver.resolve("jetson-orin@jp6.2.3")
    return replace(target, support_status="supported") if supported else target


def _verified_artifact(
    kind: str,
    filename: str,
    relative_path: str,
    content: bytes,
) -> VerifiedAcquisitionArtifact:
    sha1 = hashlib.sha1(content).hexdigest()
    return VerifiedAcquisitionArtifact(
        kind=kind,
        filename=filename,
        relative_path=relative_path,
        size=len(content),
        sha1=sha1,
        sha256=hashlib.sha256(content).hexdigest(),
        official_sha1=sha1,
    )


def _artifacts(target) -> tuple[VerifiedAcquisitionArtifact, ...]:
    inputs = target.record["construction_inputs"]
    return (
        _verified_artifact(
            "bsp",
            str(inputs["bsp"]["filename"]),
            f"jp623/{inputs['bsp']['filename']}",
            b"verified jp623 bsp",
        ),
        _verified_artifact(
            "sample_rootfs",
            str(inputs["sample_rootfs"]["filename"]),
            f"jp623/{inputs['sample_rootfs']['filename']}",
            b"verified jp623 sample rootfs",
        ),
    )


def _publish_acquisition(
    data_root: Path,
    target,
) -> tuple[VerifiedAcquisitionArtifact, ...]:
    artifacts = _artifacts(target)
    downloads = data_root / "sdkm" / "downloads"
    for artifact, content in zip(
        artifacts,
        (b"verified jp623 bsp", b"verified jp623 sample rootfs"),
    ):
        path = downloads / artifact.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    discovery = SdkManagerDiscovery(
        sdk_manager_version="2.4.1.13536",
        query_source="current",
        target=VerifiedSdkManagerTarget(
            canonical_id=target.canonical_id,
            jetpack_version="6.2.3",
            sdk_manager_display_label="JetPack 6.2.3",
            sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        ),
    )
    response = SdkManagerResponseFile(
        path=data_root / "sdkm" / "responses" / "jp623.ini",
        sha256="a" * 64,
        role_id=JP6_DEVELOPER_ROLE_V1.role_id,
        role_digest=JP6_DEVELOPER_ROLE_V1.digest(),
    )
    metadata_bytes = b"normalized sdk manager evidence"
    receipt = make_receipt(
        discovery,
        JP6_DEVELOPER_ROLE_V1,
        response,
        download_root=downloads,
        artifacts=artifacts,
        sdk_manager_metadata=(
            AcquisitionMetadataFile(
                relative_path="sdkm.json",
                size=len(metadata_bytes),
                sha256=hashlib.sha256(metadata_bytes).hexdigest(),
            ),
        ),
    )
    receipt_dir = data_root / "sdkm" / "receipts" / receipt.acquisition_digest
    metadata = receipt_dir / "metadata" / "sdkm.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(metadata_bytes)
    write_receipt_atomic(receipt_dir / "receipt.json", receipt)
    return artifacts


def _manifest(target, artifacts: tuple[VerifiedAcquisitionArtifact, ...]):
    return {
        "schema_version": 1,
        "canonical_id": target.canonical_id,
        "sdk_manager_target": "JETSON_ORIN_NX_TARGETS",
        "artifacts": [
            {
                "kind": artifact.kind,
                "filename": artifact.filename,
                "sha256": artifact.sha256,
                "size": artifact.size,
            }
            for artifact in artifacts
        ],
    }


def _publish_base(
    data_root: Path,
    target,
    artifacts: tuple[VerifiedAcquisitionArtifact, ...],
) -> Path:
    package_set = ConstructionPackageSet(
        seed=PackageSeed("nvidia-jetpack", "6.2.3+b81", "arm64"),
        repository_base="https://repo.download.nvidia.com/jetson/",
        repository_suites=("common r36.5 main", "t234 r36.5 main"),
        packages=(
            LockedPackage(
                "nvidia-jetpack",
                "6.2.3+b81",
                "arm64",
                "install",
                "nvidia-jetpack.deb",
                "b" * 64,
            ),
        ),
    )
    by_kind = {artifact.kind: artifact for artifact in artifacts}
    lock = {
        "schema_version": 1,
        "target": {
            "canonical_id": target.canonical_id,
            "jetpack_version": "6.2.3",
            "l4t_version": "36.5.2",
            "ubuntu_suite": "jammy",
            "target_abi": "aarch64",
            "debian_architecture": "arm64",
            "repository_platform": "t234",
        },
        "artifacts": {
            kind: {
                "filename": artifact.filename,
                "size": artifact.size,
                "official_sha1": artifact.official_sha1,
                "sha256": artifact.sha256,
            }
            for kind, artifact in by_kind.items()
        },
        "construction_packages": package_set.to_dict(),
        "construction": {"recipe_digest": construction_recipe_digest_v1()},
    }
    projection = build_base_target_projection_digest(lock)
    base_digest = build_base_digest(
        base_target_projection_digest=projection,
        construction_recipe_digest=construction_recipe_digest_v1(),
        artifacts=artifacts,
    )
    lock_digest = target_lock_digest(lock)
    directory = data_root / "targets" / lock_digest
    (directory / "base").mkdir(parents=True)
    lock_path = directory / "lock.json"
    manifest_path = directory / "manifest.json"
    write_target_lock(lock_path, lock)
    write_json_atomic(manifest_path, {"schema_version": 1, "base_digest": base_digest})
    receipt = make_base_receipt(
        base_digest=base_digest,
        target_lock_digest_value=lock_digest,
        base_target_projection_digest=projection,
        construction_recipe_digest=construction_recipe_digest_v1(),
        construction_package_set_digest=package_set.digest(),
        artifacts=artifacts,
        manifest_path=manifest_path,
    )
    write_base_receipt(directory / "receipt.json", receipt)
    return directory


def _snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (str(path.relative_to(root)), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )


def test_all_verified_artifacts_have_zero_download_and_reuse_base(
    tmp_path: Path,
) -> None:
    target = _target()
    artifacts = _publish_acquisition(tmp_path, target)
    index = rebuild_artifact_index(tmp_path)
    base = _publish_base(tmp_path, target, artifacts)

    first = plan_release(
        target,
        hardware_profile=PROFILE_16GB,
        artifact_index=index,
        data_root=tmp_path,
        sdk_manager_manifest=_manifest(target, artifacts),
        base_directories=(base,),
    )
    second = plan_release(
        target,
        hardware_profile=PROFILE_16GB,
        artifact_index=index,
        data_root=tmp_path,
        sdk_manager_manifest=_manifest(target, artifacts),
        base_directories=(base,),
    )

    assert first == second
    assert first.verified_cached_count == 2
    assert first.download_required_count == 0
    assert first.sdkm_decision_count == 0
    assert first.known_download_bytes == 0
    assert first.download_total_complete
    assert first.base_status is BasePlanStatus.BASE_REUSE
    assert first.base_digest is not None
    json.dumps(first.to_dict(), sort_keys=True)


def test_one_missing_artifact_adds_only_its_exact_size(tmp_path: Path) -> None:
    target = _target()
    artifacts = _publish_acquisition(tmp_path, target)
    index = rebuild_artifact_index(tmp_path)
    missing = artifacts[1]
    (tmp_path / "sdkm" / "downloads" / missing.relative_path).unlink()

    plan = plan_release(
        target,
        hardware_profile=PROFILE_16GB,
        artifact_index=index,
        data_root=tmp_path,
        sdk_manager_manifest=_manifest(target, artifacts),
    )

    assert plan.verified_cached_count == 1
    assert plan.download_required_count == 1
    assert plan.known_download_bytes == missing.size
    assert plan.unknown_download_artifact_count == 0
    assert plan.download_total_complete


def test_unknown_manifest_evidence_requires_sdkm_decision_not_fake_zero_bytes(
    tmp_path: Path,
) -> None:
    target = _target()
    unknown_manifest = {
        "schema_version": 999,
        "canonical_id": target.canonical_id,
        "sdk_manager_target": "JETSON_ORIN_NX_TARGETS",
        "artifacts": [],
    }

    plan = plan_release(
        target,
        hardware_profile=PROFILE_16GB,
        artifact_index=ArtifactIndex(schema_version=1, contents=()),
        data_root=tmp_path,
        sdk_manager_manifest=unknown_manifest,
    )

    assert plan.sdkm_decision_count == 2
    assert plan.known_download_bytes == 0
    assert plan.unknown_download_artifact_count == 2
    assert not plan.download_total_complete
    assert all(artifact.size is None for artifact in plan.artifacts)


def test_same_filename_with_different_hash_requires_download(tmp_path: Path) -> None:
    target = _target()
    artifacts = _publish_acquisition(tmp_path, target)
    index = rebuild_artifact_index(tmp_path)
    manifest = _manifest(target, artifacts)
    expected_bsp = manifest["artifacts"][0]
    expected_bsp["sha256"] = hashlib.sha256(b"new release bytes").hexdigest()

    plan = plan_release(
        target,
        hardware_profile=PROFILE_16GB,
        artifact_index=index,
        data_root=tmp_path,
        sdk_manager_manifest=manifest,
    )

    assert plan.artifacts[0].filename == artifacts[0].filename
    assert plan.artifacts[0].status is PlanArtifactStatus.DOWNLOAD_REQUIRED
    assert plan.artifacts[1].status is PlanArtifactStatus.VERIFIED_CACHED


def test_catalogued_pending_target_is_not_promoted_to_supported(tmp_path: Path) -> None:
    target = _target(supported=False)

    plan = plan_release(
        target,
        hardware_profile=PROFILE_16GB,
        artifact_index=ArtifactIndex(schema_version=1, contents=()),
        data_root=tmp_path,
    )

    assert target.support_status == "validation-pending"
    assert plan.software_target.support_status == "validation-pending"
    assert not plan.software_target.supported


def test_hardware_alias_without_semantic_change_does_not_force_new_base(
    tmp_path: Path,
) -> None:
    target = _target()
    artifacts = _publish_acquisition(tmp_path, target)
    index = rebuild_artifact_index(tmp_path)
    base = _publish_base(tmp_path, target, artifacts)
    manifest = _manifest(target, artifacts)

    first = plan_release(
        target,
        hardware_profile=PROFILE_16GB,
        artifact_index=index,
        data_root=tmp_path,
        sdk_manager_manifest=manifest,
        base_directories=(base,),
    )
    second = plan_release(
        target,
        hardware_profile=PROFILE_8GB,
        artifact_index=index,
        data_root=tmp_path,
        sdk_manager_manifest=manifest,
        base_directories=(base,),
    )

    assert first.base_status is second.base_status is BasePlanStatus.BASE_REUSE
    assert first.base_digest == second.base_digest
    assert first.artifacts == second.artifacts


def test_planner_does_not_mutate_filesystem_or_start_external_work(
    tmp_path: Path, monkeypatch
) -> None:
    target = _target()
    artifacts = _publish_acquisition(tmp_path, target)
    index = rebuild_artifact_index(tmp_path)
    base = _publish_base(tmp_path, target, artifacts)
    before = _snapshot(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("planner attempted external work")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    plan = plan_release(
        target,
        hardware_profile=PROFILE_16GB,
        artifact_index=index,
        data_root=tmp_path,
        sdk_manager_manifest=_manifest(target, artifacts),
        base_directories=(base,),
    )

    assert plan.base_status is BasePlanStatus.BASE_REUSE
    assert _snapshot(tmp_path) == before
