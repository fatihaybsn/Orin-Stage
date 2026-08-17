from __future__ import annotations

from pathlib import Path

import pytest

from orin_stage.acquisition.sdk_manager_discovery import SdkManagerDiscovery
from orin_stage.acquisition.sdk_manager_download import (
    SdkManagerDownloadPlanError,
    build_downloadonly_plan,
)
from orin_stage.acquisition.sdk_manager_match import VerifiedSdkManagerTarget


def _jp623_discovery() -> SdkManagerDiscovery:
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


def test_builds_exact_target_only_downloadonly_command() -> None:
    plan = build_downloadonly_plan(
        _jp623_discovery(),
        download_folder=Path("/srv/orin-stage/sdkm/downloads"),
        include_host=False,
    )

    assert plan.command == (
        "sdkmanager",
        "--cli",
        "--action",
        "downloadonly",
        "--login-type",
        "devzone",
        "--product",
        "Jetson",
        "--version",
        "6.2.3",
        "--target-os",
        "Linux",
        "--target",
        "JETSON_ORIN_NX_TARGETS",
        "--download-folder",
        "/srv/orin-stage/sdkm/downloads",
        "--exit-on-finish",
    )


def test_host_selection_is_explicit_when_requested() -> None:
    plan = build_downloadonly_plan(
        _jp623_discovery(),
        download_folder=Path("/srv/orin-stage/sdkm/downloads"),
        include_host=True,
    )

    assert "--host" in plan.command
    assert plan.include_host is True


def test_plan_never_injects_flash_additional_sdk_or_license_acceptance() -> None:
    plan = build_downloadonly_plan(
        _jp623_discovery(),
        download_folder=Path("/srv/orin-stage/sdkm/downloads"),
        include_host=False,
    )

    assert "--flash" not in plan.command
    assert "--additional-sdk" not in plan.command
    assert "--license" not in plan.command
    assert "--licenses" not in plan.command
    assert "--sudo-password" not in plan.command


def test_plan_preserves_discovery_evidence_for_later_receipt() -> None:
    plan = build_downloadonly_plan(
        _jp623_discovery(),
        download_folder=Path("/srv/orin-stage/sdkm/downloads"),
        include_host=False,
    )

    assert plan.canonical_id == "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
    assert plan.sdk_manager_version == "2.4.1.13536"
    assert plan.jetpack_version == "6.2.3"
    assert plan.sdk_manager_target == "JETSON_ORIN_NX_TARGETS"
    assert plan.query_source == "current"


def test_relative_download_folder_is_rejected() -> None:
    with pytest.raises(SdkManagerDownloadPlanError, match="absolute path"):
        build_downloadonly_plan(
            _jp623_discovery(),
            download_folder=Path("sdkm/downloads"),
            include_host=False,
        )


def test_empty_executable_is_rejected() -> None:
    with pytest.raises(SdkManagerDownloadPlanError, match="executable"):
        build_downloadonly_plan(
            _jp623_discovery(),
            download_folder=Path("/srv/orin-stage/sdkm/downloads"),
            include_host=False,
            executable="  ",
        )
