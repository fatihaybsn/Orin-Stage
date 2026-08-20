from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from orin_stage.acquisition.acquisition_receipt import build_acquisition_digest
from orin_stage.acquisition.artifact_verification import VerifiedAcquisitionArtifact
from orin_stage.acquisition.sdk_manager_discovery import SdkManagerDiscovery
from orin_stage.acquisition.sdk_manager_match import VerifiedSdkManagerTarget
from orin_stage.acquisition.sdk_manager_response import SdkManagerResponseFile
from orin_stage.acquisition.sdk_manager_role import JP6_DEVELOPER_ROLE_V1
from orin_stage.base import (
    BaseIdentityError,
    build_base_digest,
    build_base_target_projection_digest,
)


def _artifact(kind: str, content: bytes) -> VerifiedAcquisitionArtifact:
    sha1 = hashlib.sha1(content).hexdigest()
    return VerifiedAcquisitionArtifact(
        kind=kind,
        filename=f"{kind}.tbz2",
        relative_path=f"downloads/{kind}.tbz2",
        size=len(content),
        sha1=sha1,
        sha256=hashlib.sha256(content).hexdigest(),
        official_sha1=sha1,
    )


def _artifacts() -> tuple[VerifiedAcquisitionArtifact, ...]:
    return (
        _artifact("bsp", b"verified bsp bytes"),
        _artifact("sample_rootfs", b"verified sample rootfs bytes"),
    )


def _target_lock() -> dict[str, object]:
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
        "construction_packages": {
            "seed": {
                "name": "nvidia-jetpack",
                "version": "6.2.3+b81",
                "architecture": "arm64",
            },
            "packages": [
                {
                    "name": "cuda-toolkit-12-6",
                    "version": "12.6.3-1",
                    "architecture": "arm64",
                    "operation": "install",
                    "filename": "cuda-toolkit-12-6.deb",
                    "sha256": "a" * 64,
                }
            ],
        },
        "acquisition": {
            "sdk_manager_version": "2.4.1",
            "response_file_sha256": "b" * 64,
        },
        "construction": {
            "qemu": {"version": "qemu 8.2"},
        },
        "validation": {"policy_version": 1},
    }


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


def _response(path: Path, sha256: str) -> SdkManagerResponseFile:
    return SdkManagerResponseFile(
        path=path,
        sha256=sha256,
        role_id=JP6_DEVELOPER_ROLE_V1.role_id,
        role_digest=JP6_DEVELOPER_ROLE_V1.digest(),
    )


def test_projection_ignores_acquisition_qemu_and_validation_evidence() -> None:
    original = _target_lock()
    changed = copy.deepcopy(original)
    changed["acquisition"]["sdk_manager_version"] = "2.5.0"  # type: ignore[index]
    changed["acquisition"]["response_file_sha256"] = "c" * 64  # type: ignore[index]
    changed["construction"]["qemu"]["version"] = "qemu 9.0"  # type: ignore[index]
    changed["validation"]["policy_version"] = 2  # type: ignore[index]

    assert build_base_target_projection_digest(original) == build_base_target_projection_digest(changed)


def test_projection_changes_when_exact_package_set_changes() -> None:
    original = _target_lock()
    changed = copy.deepcopy(original)
    changed["construction_packages"]["packages"][0]["version"] = "12.6.4-1"  # type: ignore[index]

    assert build_base_target_projection_digest(original) != build_base_target_projection_digest(changed)


def test_base_digest_is_deterministic_and_artifact_order_independent() -> None:
    artifacts = _artifacts()
    kwargs = {
        "base_target_projection_digest": build_base_target_projection_digest(_target_lock()),
        "construction_recipe_digest": "2" * 64,
    }

    first = build_base_digest(artifacts=artifacts, **kwargs)
    second = build_base_digest(artifacts=reversed(artifacts), **kwargs)

    assert first == second
    assert len(first) == 64


def test_base_digest_changes_only_when_base_identity_inputs_change() -> None:
    artifacts = _artifacts()
    projection = build_base_target_projection_digest(_target_lock())
    original = build_base_digest(
        base_target_projection_digest=projection,
        construction_recipe_digest="2" * 64,
        artifacts=artifacts,
    )

    changed_projection = build_base_digest(
        base_target_projection_digest="3" * 64,
        construction_recipe_digest="2" * 64,
        artifacts=artifacts,
    )
    changed_recipe = build_base_digest(
        base_target_projection_digest=projection,
        construction_recipe_digest="4" * 64,
        artifacts=artifacts,
    )
    changed_artifact = build_base_digest(
        base_target_projection_digest=projection,
        construction_recipe_digest="2" * 64,
        artifacts=(
            _artifact("bsp", b"different bsp bytes"),
            artifacts[1],
        ),
    )

    assert len({original, changed_projection, changed_recipe, changed_artifact}) == 4


def test_base_digest_rejects_incomplete_construction_artifact_set() -> None:
    with pytest.raises(BaseIdentityError, match="exact construction artifact set"):
        build_base_digest(
            base_target_projection_digest="1" * 64,
            construction_recipe_digest="2" * 64,
            artifacts=(_artifacts()[0],),
        )


def test_acquisition_identity_can_change_without_changing_base_identity(
    tmp_path: Path,
) -> None:
    first_response = _response(tmp_path / "first.ini", "a" * 64)
    second_response = _response(tmp_path / "second.ini", "b" * 64)

    first_acquisition = build_acquisition_digest(
        _discovery(), first_response, JP6_DEVELOPER_ROLE_V1
    )
    second_acquisition = build_acquisition_digest(
        _discovery(), second_response, JP6_DEVELOPER_ROLE_V1
    )

    base_inputs = {
        "base_target_projection_digest": build_base_target_projection_digest(_target_lock()),
        "construction_recipe_digest": "2" * 64,
        "artifacts": _artifacts(),
    }
    first_base = build_base_digest(**base_inputs)
    second_base = build_base_digest(**base_inputs)

    assert first_acquisition != second_acquisition
    assert first_base == second_base
