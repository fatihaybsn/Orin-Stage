from __future__ import annotations

from pathlib import Path

import pytest

from orin_stage.acquisition.sdk_manager_discovery import (
    SdkManagerDiscovery,
    discover_catalog_target,
)
from orin_stage.acquisition.sdk_manager_match import (
    SdkManagerTargetMismatchError,
    VerifiedSdkManagerTarget,
)
from orin_stage.catalog import TargetResolver, builtin_catalog_paths


CATALOG_PATHS = builtin_catalog_paths()


def _resolver() -> TargetResolver:
    return TargetResolver(
        CATALOG_PATHS.targets_dir,
        CATALOG_PATHS.schema_path,
    )


class FakeSdkManagerClient:
    def __init__(
        self,
        *,
        current_output: str,
        archived_output: str = "",
        version: str = "2.4.1.13536",
    ) -> None:
        self.current_output = current_output
        self.archived_output = archived_output
        self.installed_version = version
        self.query_calls: list[bool] = []

    def version(self) -> str:
        return self.installed_version

    def query_jetson(self, *, archived: bool = False) -> str:
        self.query_calls.append(archived)
        return self.archived_output if archived else self.current_output


def test_discovery_verifies_jp623_from_current_sdkmanager_catalog() -> None:
    target = _resolver().resolve("jetson-orin@jp6.2.3")
    client = FakeSdkManagerClient(
        current_output="""
JetPack 6.2.3
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_AGX_ORIN_TARGETS --flash
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_ORIN_NX_TARGETS --flash
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_ORIN_NANO_TARGETS --flash
"""
    )

    result = discover_catalog_target(
        client,
        target,
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
    )

    assert result == SdkManagerDiscovery(
        sdk_manager_version="2.4.1.13536",
        query_source="current",
        target=VerifiedSdkManagerTarget(
            canonical_id="nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
            jetpack_version="6.2.3",
            sdk_manager_display_label="JetPack 6.2.3",
            sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        ),
    )
    assert client.query_calls == [False]


def test_discovery_falls_back_to_archived_only_when_release_is_absent() -> None:
    target = _resolver().resolve("jetson-orin@jp6.0")
    client = FakeSdkManagerClient(
        current_output="""
JetPack 6.2.3
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_ORIN_NX_TARGETS --flash
""",
        archived_output="""
JetPack 6.0 (rev. 2)
sdkmanager --cli --action install --product Jetson --version 6.0 --target JETSON_ORIN_NX_TARGETS --flash
""",
    )

    result = discover_catalog_target(
        client,
        target,
        required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
    )

    assert result.query_source == "archived"
    assert result.target.jetpack_version == "6.0"
    assert result.target.sdk_manager_display_label == "JetPack 6.0 (rev. 2)"
    assert client.query_calls == [False, True]


def test_current_target_mismatch_is_not_hidden_by_archive_fallback() -> None:
    target = _resolver().resolve("jetson-orin@jp6.2.3")
    client = FakeSdkManagerClient(
        current_output="""
JetPack 6.2.3
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_AGX_ORIN_TARGETS --flash
""",
        archived_output="""
JetPack 6.2.3
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_ORIN_NX_TARGETS --flash
""",
    )

    with pytest.raises(SdkManagerTargetMismatchError):
        discover_catalog_target(
            client,
            target,
            required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        )

    assert client.query_calls == [False]


def test_discovery_fails_if_release_is_missing_from_current_and_archive() -> None:
    target = _resolver().resolve("jetson-orin@jp6.0")
    client = FakeSdkManagerClient(
        current_output="""
JetPack 6.2.3
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_ORIN_NX_TARGETS --flash
""",
        archived_output="""
JetPack 5.1.5
sdkmanager --cli --action install --product Jetson --version 5.1.5 --target JETSON_ORIN_NX_TARGETS --flash
""",
    )

    with pytest.raises(LookupError, match="JetPack 6.0"):
        discover_catalog_target(
            client,
            target,
            required_sdk_manager_target="JETSON_ORIN_NX_TARGETS",
        )

    assert client.query_calls == [False, True]
