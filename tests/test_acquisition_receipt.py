from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from orin_stage.acquisition.acquisition_receipt import (
    AcquisitionMetadataFile,
    build_acquisition_digest,
    make_receipt,
    receipt_is_cache_hit,
    write_receipt_atomic,
)
from orin_stage.acquisition.artifact_verification import VerifiedAcquisitionArtifact
from orin_stage.acquisition.sdk_manager_discovery import SdkManagerDiscovery
from orin_stage.acquisition.sdk_manager_match import VerifiedSdkManagerTarget
from orin_stage.acquisition.sdk_manager_response import SdkManagerResponseFile
from orin_stage.acquisition.sdk_manager_role import JP6_DEVELOPER_ROLE_V1


def _discovery() -> SdkManagerDiscovery:
    return SdkManagerDiscovery(
        sdk_manager_version="2.4.1.13536",
        query_source="current",
        target=VerifiedSdkManagerTarget(
            canonical_id="nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
            jetpack_version="6.2.3",
            sdk_manager_display_label="JetPack 6.2.3",
            sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        ),
    )


def test_acquisition_digest_changes_with_response_identity(tmp_path: Path) -> None:
    first = SdkManagerResponseFile(
        path=tmp_path / "a.ini",
        sha256="a" * 64,
        role_id=JP6_DEVELOPER_ROLE_V1.role_id,
        role_digest=JP6_DEVELOPER_ROLE_V1.digest(),
    )
    second = SdkManagerResponseFile(
        path=tmp_path / "b.ini",
        sha256="b" * 64,
        role_id=JP6_DEVELOPER_ROLE_V1.role_id,
        role_digest=JP6_DEVELOPER_ROLE_V1.digest(),
    )

    assert build_acquisition_digest(
        _discovery(), first, JP6_DEVELOPER_ROLE_V1
    ) != build_acquisition_digest(_discovery(), second, JP6_DEVELOPER_ROLE_V1)


def test_receipt_cache_hit_requires_artifact_and_sdkm_metadata_hashes(
    tmp_path: Path,
) -> None:
    downloads = tmp_path / "sdkm" / "downloads"
    receipt_dir = tmp_path / "sdkm" / "receipts" / "abc"
    metadata = receipt_dir / "metadata"
    downloads.mkdir(parents=True)
    metadata.mkdir(parents=True)

    artifact_bytes = b"artifact"
    metadata_bytes = b"sdkm response export"
    (downloads / "artifact.tbz2").write_bytes(artifact_bytes)
    (metadata / "export.ini").write_bytes(metadata_bytes)

    response = SdkManagerResponseFile(
        path=(tmp_path / "sdkm" / "responses" / "x.ini"),
        sha256="a" * 64,
        role_id=JP6_DEVELOPER_ROLE_V1.role_id,
        role_digest=JP6_DEVELOPER_ROLE_V1.digest(),
    )
    digest = build_acquisition_digest(_discovery(), response, JP6_DEVELOPER_ROLE_V1)
    receipt = make_receipt(
        _discovery(),
        JP6_DEVELOPER_ROLE_V1,
        response,
        download_root=downloads,
        artifacts=(
            VerifiedAcquisitionArtifact(
                kind="bsp",
                filename="artifact.tbz2",
                relative_path="artifact.tbz2",
                size=len(artifact_bytes),
                sha1=hashlib.sha1(artifact_bytes).hexdigest(),
                sha256=hashlib.sha256(artifact_bytes).hexdigest(),
                official_sha1=hashlib.sha1(artifact_bytes).hexdigest(),
            ),
        ),
        sdk_manager_metadata=(
            AcquisitionMetadataFile(
                relative_path="export.ini",
                size=len(metadata_bytes),
                sha256=hashlib.sha256(metadata_bytes).hexdigest(),
            ),
        ),
        now=lambda: datetime(2026, 8, 18, tzinfo=timezone.utc),
    )
    receipt_path = receipt_dir / "receipt.json"
    write_receipt_atomic(receipt_path, receipt)

    assert receipt.acquisition_digest == digest
    assert receipt_is_cache_hit(
        receipt_path, expected_digest=digest, download_root=downloads
    )

    (downloads / "artifact.tbz2").write_bytes(b"tampered")
    assert not receipt_is_cache_hit(
        receipt_path, expected_digest=digest, download_root=downloads
    )
