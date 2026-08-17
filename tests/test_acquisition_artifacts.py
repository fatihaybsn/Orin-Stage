from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from orin_stage.acquisition.artifact_verification import (
    AcquisitionArtifactAmbiguousError,
    AcquisitionArtifactChecksumError,
    AcquisitionArtifactMissingError,
    verify_catalog_construction_artifacts,
    verify_receipt_artifact,
)
from orin_stage.catalog.resolver import ResolvedCatalogTarget


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _target(bsp: bytes, rootfs: bytes) -> ResolvedCatalogTarget:
    return ResolvedCatalogTarget(
        selector="jetson-orin@jp6.2.3",
        canonical_id="nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
        aliases=("jetson-orin@jp6.2.3",),
        support_status="validation-pending",
        source_path=Path("catalog/targets/jp6.2.3.yaml"),
        record={
            "construction_inputs": {
                "bsp": {"filename": "Jetson_Linux_R36.5.2_aarch64.tbz2"},
                "sample_rootfs": {
                    "filename": "Tegra_Linux_Sample-Root-Filesystem_R36.5.2_aarch64.tbz2"
                },
            },
            "checksums": {
                "official": {
                    "algorithm": "sha1",
                    "artifacts": {
                        "bsp": {
                            "filename": "Jetson_Linux_R36.5.2_aarch64.tbz2",
                            "digest": _sha1(bsp),
                        },
                        "sample_rootfs": {
                            "filename": "Tegra_Linux_Sample-Root-Filesystem_R36.5.2_aarch64.tbz2",
                            "digest": _sha1(rootfs),
                        },
                    },
                }
            },
        },
    )


def test_catalog_construction_artifacts_get_official_sha1_and_local_sha256(
    tmp_path: Path,
) -> None:
    bsp = b"official-bsp"
    rootfs = b"official-rootfs"
    (tmp_path / "Jetson_Linux_R36.5.2_aarch64.tbz2").write_bytes(bsp)
    (tmp_path / "Tegra_Linux_Sample-Root-Filesystem_R36.5.2_aarch64.tbz2").write_bytes(rootfs)

    artifacts = verify_catalog_construction_artifacts(
        _target(bsp, rootfs), download_root=tmp_path.resolve()
    )

    assert tuple(item.kind for item in artifacts) == ("bsp", "sample_rootfs")
    assert artifacts[0].sha1 == _sha1(bsp)
    assert artifacts[0].sha256 == hashlib.sha256(bsp).hexdigest()
    assert artifacts[0].size == len(bsp)


def test_bad_nvidia_sha1_is_rejected(tmp_path: Path) -> None:
    bsp = b"official-bsp"
    rootfs = b"official-rootfs"
    (tmp_path / "Jetson_Linux_R36.5.2_aarch64.tbz2").write_bytes(b"tampered")
    (tmp_path / "Tegra_Linux_Sample-Root-Filesystem_R36.5.2_aarch64.tbz2").write_bytes(rootfs)

    with pytest.raises(AcquisitionArtifactChecksumError, match="SHA-1 mismatch"):
        verify_catalog_construction_artifacts(
            _target(bsp, rootfs), download_root=tmp_path.resolve()
        )


def test_missing_artifact_is_rejected(tmp_path: Path) -> None:
    bsp = b"official-bsp"
    rootfs = b"official-rootfs"
    (tmp_path / "Jetson_Linux_R36.5.2_aarch64.tbz2").write_bytes(bsp)

    with pytest.raises(AcquisitionArtifactMissingError):
        verify_catalog_construction_artifacts(
            _target(bsp, rootfs), download_root=tmp_path.resolve()
        )


def test_duplicate_filename_in_shared_root_is_rejected(tmp_path: Path) -> None:
    bsp = b"official-bsp"
    rootfs = b"official-rootfs"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for directory in (tmp_path / "a", tmp_path / "b"):
        (directory / "Jetson_Linux_R36.5.2_aarch64.tbz2").write_bytes(bsp)
    (tmp_path / "Tegra_Linux_Sample-Root-Filesystem_R36.5.2_aarch64.tbz2").write_bytes(rootfs)

    with pytest.raises(AcquisitionArtifactAmbiguousError):
        verify_catalog_construction_artifacts(
            _target(bsp, rootfs), download_root=tmp_path.resolve()
        )


def test_receipt_artifact_gate_detects_byte_change(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"same")
    digest = hashlib.sha256(b"same").hexdigest()

    assert verify_receipt_artifact(
        tmp_path,
        relative_path="artifact.bin",
        expected_size=4,
        expected_sha256=digest,
    )

    path.write_bytes(b"evil")
    assert not verify_receipt_artifact(
        tmp_path,
        relative_path="artifact.bin",
        expected_size=4,
        expected_sha256=digest,
    )


def test_receipt_artifact_gate_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.bin"
    real.write_bytes(b"same")
    link = tmp_path / "link.bin"
    link.symlink_to(real.name)

    assert not verify_receipt_artifact(
        tmp_path,
        relative_path="link.bin",
        expected_size=4,
        expected_sha256=hashlib.sha256(b"same").hexdigest(),
    )
