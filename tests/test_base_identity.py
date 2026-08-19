from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from orin_stage.acquisition.acquisition_receipt import build_acquisition_digest
from orin_stage.acquisition.artifact_verification import VerifiedAcquisitionArtifact
from orin_stage.acquisition.sdk_manager_discovery import SdkManagerDiscovery
from orin_stage.acquisition.sdk_manager_match import VerifiedSdkManagerTarget
from orin_stage.acquisition.sdk_manager_response import SdkManagerResponseFile
from orin_stage.acquisition.sdk_manager_role import JP6_DEVELOPER_ROLE_V1
from orin_stage.base import BaseIdentityError, build_base_digest


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


def test_base_digest_is_deterministic_and_artifact_order_independent() -> None:
    artifacts = _artifacts()
    kwargs = {
        "target_lock_digest": "1" * 64,
        "construction_recipe_digest": "2" * 64,
    }

    first = build_base_digest(artifacts=artifacts, **kwargs)
    second = build_base_digest(artifacts=reversed(artifacts), **kwargs)

    assert first == second
    assert len(first) == 64


def test_base_digest_changes_only_when_base_identity_inputs_change() -> None:
    artifacts = _artifacts()
    original = build_base_digest(
        target_lock_digest="1" * 64,
        construction_recipe_digest="2" * 64,
        artifacts=artifacts,
    )

    changed_lock = build_base_digest(
        target_lock_digest="3" * 64,
        construction_recipe_digest="2" * 64,
        artifacts=artifacts,
    )
    changed_recipe = build_base_digest(
        target_lock_digest="1" * 64,
        construction_recipe_digest="4" * 64,
        artifacts=artifacts,
    )
    changed_artifact = build_base_digest(
        target_lock_digest="1" * 64,
        construction_recipe_digest="2" * 64,
        artifacts=(
            _artifact("bsp", b"different bsp bytes"),
            artifacts[1],
        ),
    )

    assert len({original, changed_lock, changed_recipe, changed_artifact}) == 4


def test_base_digest_rejects_incomplete_construction_artifact_set() -> None:
    with pytest.raises(BaseIdentityError, match="exact construction artifact set"):
        build_base_digest(
            target_lock_digest="1" * 64,
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
        "target_lock_digest": "1" * 64,
        "construction_recipe_digest": "2" * 64,
        "artifacts": _artifacts(),
    }
    first_base = build_base_digest(**base_inputs)
    second_base = build_base_digest(**base_inputs)

    assert first_acquisition != second_acquisition
    assert first_base == second_base
