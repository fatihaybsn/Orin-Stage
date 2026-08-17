from __future__ import annotations

import json
from pathlib import Path

import pytest

from orin_stage.acquisition.sdk_manager_discovery import SdkManagerDiscovery
from orin_stage.acquisition.sdk_manager_manifest import (
    SdkManagerManifestError,
    copy_sdk_manager_reference_files,
    find_sdk_manager_reference_files,
)
from orin_stage.acquisition.sdk_manager_match import VerifiedSdkManagerTarget


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


def _write_reference_tree(root: Path) -> tuple[Path, Path]:
    software = root / "dist" / "sdkml3_jetpack_623.json"
    hardware = root / "hwdata" / "families" / "series" / "orin-nx.json"
    software.parent.mkdir(parents=True)
    hardware.parent.mkdir(parents=True)
    software.write_text(
        json.dumps(
            {
                "release": {
                    "releaseVersion": "6.2.3",
                    "targetHW": ["JETSON_ORIN_NX_TARGETS"],
                }
            }
        ),
        encoding="utf-8",
    )
    hardware.write_text(
        json.dumps({"hw": {"x": {"id": "JETSON_ORIN_NX_TARGETS"}}}),
        encoding="utf-8",
    )
    return software, hardware


def test_exact_sdkmanager_software_and_hardware_manifests_are_found(tmp_path: Path) -> None:
    software, hardware = _write_reference_tree(tmp_path)

    assert find_sdk_manager_reference_files(tmp_path, _discovery()) == (
        software,
        hardware,
    )


def test_reference_manifests_are_copied_as_receipt_evidence(tmp_path: Path) -> None:
    _write_reference_tree(tmp_path / "state")
    destination = tmp_path / "receipt" / "metadata"

    copied = copy_sdk_manager_reference_files(
        tmp_path / "state", _discovery(), destination=destination
    )

    assert len(copied) == 2
    assert copied[0].name.startswith("software--")
    assert copied[1].name.startswith("hardware--")


def test_ambiguous_software_manifest_is_rejected(tmp_path: Path) -> None:
    software, _ = _write_reference_tree(tmp_path)
    duplicate = software.parent / "sdkml3_duplicate.json"
    duplicate.write_bytes(software.read_bytes())

    with pytest.raises(SdkManagerManifestError, match="exactly one"):
        find_sdk_manager_reference_files(tmp_path, _discovery())
