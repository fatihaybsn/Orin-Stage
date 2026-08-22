from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from orin_stage.acquisition.acquisition_receipt import load_receipt
from orin_stage.acquisition.artifact_verification import AcquisitionArtifactChecksumError
from orin_stage.acquisition.sdk_manager import SdkManagerClient
from orin_stage.acquisition.sdk_manager_acquisition import ensure_sdk_manager_acquisition
from orin_stage.acquisition.sdk_manager_adoption import adopt_sdk_manager_acquisition
from orin_stage.acquisition.sdk_manager_match import SdkManagerTargetMismatchError
from orin_stage.catalog.resolver import TargetResolver


ROOT = Path(__file__).resolve().parents[1]
BSP_NAME = "Jetson_Linux_R36.5.2_aarch64.tbz2"
ROOTFS_NAME = "Tegra_Linux_Sample-Root-Filesystem_R36.5.2_aarch64.tbz2"


class FakeSdkManagerClient(SdkManagerClient):
    def version(self) -> str:
        return "2.4.1.13536"

    def query_jetson(self, *, archived: bool = False) -> str:
        assert archived is False
        return """
JetPack 6.2.3
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_ORIN_NX_TARGETS
"""


def _target_for_bytes(bsp: bytes, rootfs: bytes):
    resolver = TargetResolver(
        targets_dir=ROOT / "catalog" / "targets",
        schema_path=ROOT / "catalog" / "schema" / "target.schema.json",
    )
    target = resolver.resolve("jetson-orin@jp6.2.3")
    record = copy.deepcopy(target.record)
    record["checksums"]["official"]["artifacts"]["bsp"]["digest"] = (
        hashlib.sha1(bsp).hexdigest()
    )
    record["checksums"]["official"]["artifacts"]["sample_rootfs"]["digest"] = (
        hashlib.sha1(rootfs).hexdigest()
    )
    return replace(target, record=record)


def _downloads(root: Path, bsp: bytes, rootfs: bytes) -> Path:
    root.mkdir(parents=True)
    (root / BSP_NAME).write_bytes(bsp)
    (root / ROOTFS_NAME).write_bytes(rootfs)
    return root


def _sdkm_state(root: Path) -> Path:
    software = root / "dist" / "sdkml3_jp623.json"
    hardware = root / "hwdata" / "orin-nx.json"
    software.parent.mkdir(parents=True)
    hardware.parent.mkdir(parents=True)
    software.write_text(
        json.dumps(
            {
                "release": {
                    "releaseVersion": "6.2.3",
                    "targets": ["JETSON_ORIN_NX_TARGETS"],
                }
            }
        ),
        encoding="utf-8",
    )
    hardware.write_text(
        json.dumps({"hardware": {"id": "JETSON_ORIN_NX_TARGETS"}}),
        encoding="utf-8",
    )
    return root


def test_adopts_with_hardlinks_and_existing_ensure_becomes_cache_hit(
    tmp_path: Path,
) -> None:
    bsp = b"official bsp"
    rootfs = b"official sample rootfs"
    source = _downloads(tmp_path / "existing", bsp, rootfs).resolve()
    data_root = (tmp_path / "data").resolve()
    target = _target_for_bytes(bsp, rootfs)

    adopted = adopt_sdk_manager_acquisition(
        FakeSdkManagerClient(),
        target,
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        data_root=data_root,
        existing_download_folder=source,
        sdk_manager_state_root=tmp_path / "missing-state",
    )

    assert adopted.cache_hit is False
    assert adopted.receipt is not None
    receipt = load_receipt(adopted.receipt_path)
    assert receipt["selected_groups"] == [
        "Jetson Linux",
        "Jetson Runtime Components",
        "Jetson SDK Components",
    ]
    for item in receipt["artifacts"]:
        managed = Path(receipt["download_root"]) / item["relative_path"]
        original = source / item["filename"]
        assert managed.stat().st_ino == original.stat().st_ino
        assert item["size"] == original.stat().st_size
        assert item["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()

    def forbidden_download(_plan) -> None:
        raise AssertionError("downloadonly must not run for an adopted cache hit")

    ensured = ensure_sdk_manager_acquisition(
        FakeSdkManagerClient(),
        target,
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        data_root=data_root,
        execute=forbidden_download,
    )
    assert ensured.cache_hit is True
    assert ensured.receipt_path == adopted.receipt_path


def test_adoption_includes_matching_nvsdkm_reference_metadata(tmp_path: Path) -> None:
    bsp, rootfs = b"bsp", b"rootfs"
    source = _downloads(tmp_path / "existing", bsp, rootfs).resolve()
    state = _sdkm_state(tmp_path / "nvsdkm")

    result = adopt_sdk_manager_acquisition(
        FakeSdkManagerClient(),
        _target_for_bytes(bsp, rootfs),
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        data_root=(tmp_path / "data").resolve(),
        existing_download_folder=source,
        sdk_manager_state_root=state,
    )

    receipt = load_receipt(result.receipt_path)
    paths = {item["relative_path"] for item in receipt["sdk_manager_metadata"]}
    assert "adoption.json" in paths
    assert any(path.startswith("sdkmanager-reference/software--") for path in paths)
    assert any(path.startswith("sdkmanager-reference/hardware--") for path in paths)


def test_corrupt_existing_artifact_is_rejected_without_publish(tmp_path: Path) -> None:
    bsp, rootfs = b"bsp", b"rootfs"
    source = _downloads(tmp_path / "existing", b"tampered", rootfs).resolve()
    data_root = (tmp_path / "data").resolve()

    with pytest.raises(AcquisitionArtifactChecksumError, match="NVIDIA SHA-1 mismatch"):
        adopt_sdk_manager_acquisition(
            FakeSdkManagerClient(),
            _target_for_bytes(bsp, rootfs),
            required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
            data_root=data_root,
            existing_download_folder=source,
        )

    assert not (data_root / "sdkm").exists()


def test_cross_filesystem_fallback_copies_into_atomic_managed_directory(
    tmp_path: Path, monkeypatch
) -> None:
    bsp, rootfs = b"bsp", b"rootfs"
    source = _downloads(tmp_path / "existing", bsp, rootfs).resolve()
    original_link = os.link

    def cross_device_link(src, dst, *args, **kwargs):
        if Path(src).parent == source:
            raise OSError(errno.EXDEV, "cross-device link")
        return original_link(src, dst, *args, **kwargs)

    monkeypatch.setattr("orin_stage.acquisition.sdk_manager_adoption.os.link", cross_device_link)
    result = adopt_sdk_manager_acquisition(
        FakeSdkManagerClient(),
        _target_for_bytes(bsp, rootfs),
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        data_root=(tmp_path / "data").resolve(),
        existing_download_folder=source,
        sdk_manager_state_root=tmp_path / "missing-state",
    )

    receipt = load_receipt(result.receipt_path)
    for item in receipt["artifacts"]:
        managed = Path(receipt["download_root"]) / item["relative_path"]
        assert managed.stat().st_ino != (source / item["filename"]).stat().st_ino
    metadata_root = result.receipt_path.parent / "metadata"
    evidence = json.loads((metadata_root / "adoption.json").read_text(encoding="utf-8"))
    assert set(evidence["transfer_methods"].values()) == {"copy-cross-filesystem"}


def test_query_target_mismatch_is_rejected_before_managed_publish(tmp_path: Path) -> None:
    bsp, rootfs = b"bsp", b"rootfs"
    source = _downloads(tmp_path / "existing", bsp, rootfs).resolve()

    class WrongTargetClient(FakeSdkManagerClient):
        def query_jetson(self, *, archived: bool = False) -> str:
            assert archived is False
            return """
JetPack 6.2.3
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_AGX_ORIN_TARGETS
"""

    data_root = (tmp_path / "data").resolve()
    with pytest.raises(SdkManagerTargetMismatchError, match="JETSON_ORIN_NX_TARGETS"):
        adopt_sdk_manager_acquisition(
            WrongTargetClient(),
            _target_for_bytes(bsp, rootfs),
            required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
            data_root=data_root,
            existing_download_folder=source,
        )

    assert not list((data_root / "sdkm" / "receipts").glob("*/receipt.json"))
    assert not list((data_root / "sdkm" / "downloads").iterdir())


def test_failed_staging_leaves_no_partial_artifacts_or_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    bsp, rootfs = b"bsp", b"rootfs"
    source = _downloads(tmp_path / "existing", bsp, rootfs).resolve()
    calls = 0

    def fail_second_link(_source: Path, _destination: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated staging failure")
        os.link(_source, _destination)
        return "hardlink"

    monkeypatch.setattr(
        "orin_stage.acquisition.sdk_manager_adoption._link_or_copy",
        fail_second_link,
    )
    data_root = (tmp_path / "data").resolve()
    with pytest.raises(OSError, match="simulated staging failure"):
        adopt_sdk_manager_acquisition(
            FakeSdkManagerClient(),
            _target_for_bytes(bsp, rootfs),
            required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
            data_root=data_root,
            existing_download_folder=source,
            sdk_manager_state_root=tmp_path / "missing-state",
        )

    sdkm = data_root / "sdkm"
    assert not list(sdkm.glob(".adoption-artifacts-*"))
    assert not list((sdkm / "downloads").iterdir())
    assert not list((sdkm / "receipts").iterdir())
