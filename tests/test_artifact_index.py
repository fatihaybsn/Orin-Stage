from __future__ import annotations

import hashlib
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
from orin_stage.planning import (
    ArtifactExpectation,
    PlanArtifactStatus,
    artifact_index_path,
    artifact_status,
    load_artifact_index,
    rebuild_artifact_index,
)


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


def _publish_receipt(
    data_root: Path,
    *,
    canonical_id: str,
    jetpack_version: str,
    artifacts: tuple[tuple[str, str, str, bytes], ...],
) -> Path:
    downloads = data_root / "sdkm" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    verified: list[VerifiedAcquisitionArtifact] = []
    for kind, filename, relative_path, content in artifacts:
        path = downloads / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        verified.append(_verified_artifact(kind, filename, relative_path, content))

    discovery = SdkManagerDiscovery(
        sdk_manager_version="2.4.1.13536",
        query_source="current",
        target=VerifiedSdkManagerTarget(
            canonical_id=canonical_id,
            jetpack_version=jetpack_version,
            sdk_manager_display_label=f"JetPack {jetpack_version}",
            sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        ),
    )
    response = SdkManagerResponseFile(
        path=data_root / "sdkm" / "responses" / f"{jetpack_version}.ini",
        sha256=hashlib.sha256(canonical_id.encode("utf-8")).hexdigest(),
        role_id=JP6_DEVELOPER_ROLE_V1.role_id,
        role_digest=JP6_DEVELOPER_ROLE_V1.digest(),
    )
    receipt = make_receipt(
        discovery,
        JP6_DEVELOPER_ROLE_V1,
        response,
        download_root=downloads,
        artifacts=tuple(verified),
        sdk_manager_metadata=(
            AcquisitionMetadataFile(
                relative_path="sdkm.json",
                size=len(canonical_id.encode("utf-8")),
                sha256=hashlib.sha256(canonical_id.encode("utf-8")).hexdigest(),
            ),
        ),
    )
    receipt_dir = data_root / "sdkm" / "receipts" / receipt.acquisition_digest
    metadata = receipt_dir / "metadata" / "sdkm.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(canonical_id.encode("utf-8"))
    receipt_path = receipt_dir / "receipt.json"
    write_receipt_atomic(receipt_path, receipt)
    return receipt_path


def _release_artifacts(
    release: str,
    *,
    bsp_filename: str = "Jetson_Linux.tbz2",
    bsp_content: bytes | None = None,
) -> tuple[tuple[str, str, str, bytes], ...]:
    return (
        (
            "bsp",
            bsp_filename,
            f"{release}/{bsp_filename}",
            bsp_content if bsp_content is not None else f"bsp-{release}".encode(),
        ),
        (
            "sample_rootfs",
            "Tegra_Linux_Sample-Root-Filesystem.tbz2",
            f"{release}/Tegra_Linux_Sample-Root-Filesystem.tbz2",
            f"rootfs-{release}".encode(),
        ),
    )


def _expectation(
    canonical_id: str,
    artifact: VerifiedAcquisitionArtifact,
) -> ArtifactExpectation:
    return ArtifactExpectation(
        canonical_id=canonical_id,
        artifact_kind=artifact.kind,
        filename=artifact.filename,
        sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        expected_sha256=artifact.sha256,
        expected_size=artifact.size,
        required_component_groups=("Jetson Linux",),
    )


def test_receipts_rebuild_deterministic_index_with_required_relationships(
    tmp_path: Path,
) -> None:
    canonical_id = "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
    artifacts = _release_artifacts("jp623")
    receipt_path = _publish_receipt(
        tmp_path,
        canonical_id=canonical_id,
        jetpack_version="6.2.3",
        artifacts=artifacts,
    )

    first = rebuild_artifact_index(tmp_path)
    second = rebuild_artifact_index(tmp_path)

    assert first == second == load_artifact_index(tmp_path)
    assert artifact_index_path(tmp_path) == tmp_path / "acquisition" / "artifact-index.json"
    relationship = first.contents[0].relationships[0]
    assert relationship.source == "sdk-manager"
    assert relationship.canonical_id == canonical_id
    assert relationship.sdk_manager_target == "JETSON_ORIN_NX_TARGETS"
    assert "Jetson Linux" in relationship.component_groups
    assert relationship.local_path.startswith("sdkm/downloads/")
    assert relationship.receipt_reference == str(receipt_path.relative_to(tmp_path))


def test_same_filename_with_different_bytes_is_not_a_cache_hit(tmp_path: Path) -> None:
    first_id = "nvidia.jetpack-6.2.2.jetson-linux-36.5.0"
    second_id = "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
    first_rows = _release_artifacts("jp622", bsp_content=b"first bytes")
    second_rows = _release_artifacts("jp623", bsp_content=b"second bytes")
    _publish_receipt(
        tmp_path,
        canonical_id=first_id,
        jetpack_version="6.2.2",
        artifacts=first_rows,
    )
    _publish_receipt(
        tmp_path,
        canonical_id=second_id,
        jetpack_version="6.2.3",
        artifacts=second_rows,
    )
    index = rebuild_artifact_index(tmp_path)
    first_artifact = _verified_artifact(*first_rows[0])
    second_artifact = _verified_artifact(*second_rows[0])

    wrong_bytes = ArtifactExpectation(
        canonical_id=first_id,
        artifact_kind=first_artifact.kind,
        filename=first_artifact.filename,
        sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        expected_sha256=second_artifact.sha256,
        expected_size=second_artifact.size,
    )

    assert artifact_status(
        index, _expectation(first_id, first_artifact), data_root=tmp_path
    ) is PlanArtifactStatus.VERIFIED_CACHED
    assert artifact_status(
        index, wrong_bytes, data_root=tmp_path
    ) is PlanArtifactStatus.DOWNLOAD_REQUIRED


def test_same_sha256_across_releases_has_one_identity_and_two_relationships(
    tmp_path: Path,
) -> None:
    shared = b"same official bsp bytes"
    first_id = "nvidia.jetpack-6.2.2.jetson-linux-36.5.0"
    second_id = "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
    _publish_receipt(
        tmp_path,
        canonical_id=first_id,
        jetpack_version="6.2.2",
        artifacts=_release_artifacts("jp622", bsp_content=shared),
    )
    _publish_receipt(
        tmp_path,
        canonical_id=second_id,
        jetpack_version="6.2.3",
        artifacts=_release_artifacts("jp623", bsp_content=shared),
    )

    index = rebuild_artifact_index(tmp_path)
    shared_digest = hashlib.sha256(shared).hexdigest()
    content = next(item for item in index.contents if item.sha256 == shared_digest)

    assert len(content.relationships) == 2
    assert {item.canonical_id for item in content.relationships} == {first_id, second_id}
    assert len({item.local_path for item in content.relationships}) == 2


def test_missing_or_changed_file_is_not_a_cache_hit(tmp_path: Path) -> None:
    canonical_id = "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
    rows = _release_artifacts("jp623")
    _publish_receipt(
        tmp_path,
        canonical_id=canonical_id,
        jetpack_version="6.2.3",
        artifacts=rows,
    )
    artifact = _verified_artifact(*rows[0])
    index = rebuild_artifact_index(tmp_path)
    path = tmp_path / "sdkm" / "downloads" / rows[0][2]

    path.unlink()
    assert artifact_status(
        index, _expectation(canonical_id, artifact), data_root=tmp_path
    ) is PlanArtifactStatus.DOWNLOAD_REQUIRED

    path.write_bytes(b"changed")
    assert artifact_status(
        index, _expectation(canonical_id, artifact), data_root=tmp_path
    ) is PlanArtifactStatus.DOWNLOAD_REQUIRED


def test_missing_expected_digest_requires_sdk_manager_decision(tmp_path: Path) -> None:
    index = rebuild_artifact_index(tmp_path)
    expectation = ArtifactExpectation(
        canonical_id="nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
        artifact_kind="bsp",
        filename="Jetson_Linux.tbz2",
        sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        expected_sha256=None,
        required_component_groups=("Jetson Linux",),
    )

    assert artifact_status(
        index, expectation, data_root=tmp_path
    ) is PlanArtifactStatus.SDKM_DECISION


def test_deleted_index_rebuilds_to_identical_canonical_json(tmp_path: Path) -> None:
    _publish_receipt(
        tmp_path,
        canonical_id="nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
        jetpack_version="6.2.3",
        artifacts=_release_artifacts("jp623"),
    )
    rebuild_artifact_index(tmp_path)
    path = artifact_index_path(tmp_path)
    first = path.read_bytes()

    path.unlink()
    rebuild_artifact_index(tmp_path)

    assert path.read_bytes() == first


def test_invalid_receipt_does_not_contribute_to_index(tmp_path: Path) -> None:
    receipt_path = _publish_receipt(
        tmp_path,
        canonical_id="nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
        jetpack_version="6.2.3",
        artifacts=_release_artifacts("jp623"),
    )
    (receipt_path.parent / "metadata" / "sdkm.json").write_bytes(b"tampered")

    assert rebuild_artifact_index(tmp_path).contents == ()
