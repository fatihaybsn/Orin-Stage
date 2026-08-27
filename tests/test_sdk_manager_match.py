from __future__ import annotations

from pathlib import Path

import pytest

from orin_stage.acquisition.sdk_manager_match import (
    SdkManagerTargetMismatchError,
    VerifiedSdkManagerTarget,
    verify_catalog_target_advertised,
)
from orin_stage.acquisition.sdk_manager_query import (
    SdkManagerJetsonRelease,
    parse_jetson_query_output,
)
from orin_stage.catalog import TargetResolver, builtin_catalog_paths


CATALOG_PATHS = builtin_catalog_paths()


def _resolver() -> TargetResolver:
    return TargetResolver(
        targets_dir=CATALOG_PATHS.targets_dir,
        schema_path=CATALOG_PATHS.schema_path,
    )


def test_jp623_catalog_target_matches_real_sdkmanager_nx_identity() -> None:
    target = _resolver().resolve("jetson-orin@jp6.2.3")
    raw = """
JetPack 6.2.3
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_AGX_ORIN_TARGETS --flash
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_ORIN_NX_TARGETS --flash
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_ORIN_NANO_TARGETS --flash
"""
    releases = parse_jetson_query_output(raw)

    verified = verify_catalog_target_advertised(
        target,
        releases,
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
    )

    assert verified == VerifiedSdkManagerTarget(
        canonical_id="nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
        jetpack_version="6.2.3",
        sdk_manager_display_label="JetPack 6.2.3",
        sdk_manager_target="JETSON_ORIN_NX_TARGETS",
    )


def test_validation_pending_catalog_target_can_be_checked_during_acquisition() -> None:
    target = _resolver().resolve("jetson-orin@jp6.2.3")
    assert target.is_validation_pending

    releases = (
        SdkManagerJetsonRelease(
            "JetPack 6.2.3",
            "6.2.3",
            ("JETSON_ORIN_NX_TARGETS",),
        ),
    )

    verified = verify_catalog_target_advertised(
        target,
        releases,
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
    )

    assert verified.jetpack_version == "6.2.3"


def test_wrong_sdkmanager_target_is_rejected_even_when_version_exists() -> None:
    target = _resolver().resolve("jetson-orin@jp6.2.3")
    releases = (
        SdkManagerJetsonRelease(
            "JetPack 6.2.3",
            "6.2.3",
            ("JETSON_AGX_ORIN_TARGETS", "JETSON_ORIN_NANO_TARGETS"),
        ),
    )

    with pytest.raises(SdkManagerTargetMismatchError) as caught:
        verify_catalog_target_advertised(
            target,
            releases,
            required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        )

    error = caught.value
    assert error.canonical_id == "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
    assert error.jetpack_version == "6.2.3"
    assert error.required_target == "JETSON_ORIN_NX_TARGETS"
    assert error.advertised_targets == (
        "JETSON_AGX_ORIN_TARGETS",
        "JETSON_ORIN_NANO_TARGETS",
    )


def test_catalog_version_must_match_sdkmanager_exactly() -> None:
    target = _resolver().resolve("jetson-orin@jp6.2.3")
    releases = (
        SdkManagerJetsonRelease(
            "JetPack 6.2",
            "6.2",
            ("JETSON_ORIN_NX_TARGETS",),
        ),
    )

    with pytest.raises(LookupError, match="JetPack 6.2.3"):
        verify_catalog_target_advertised(
            target,
            releases,
            required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        )
