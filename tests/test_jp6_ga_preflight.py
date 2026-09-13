from __future__ import annotations

from pathlib import Path

import pytest

from orin_stage.acquisition.sdk_manager_discovery import discover_catalog_target
from orin_stage.acquisition.sdk_manager_response import render_response_file
from orin_stage.acquisition.sdk_manager_role import sdk_manager_component_role
from orin_stage.catalog import TargetResolver, builtin_catalog_paths


CATALOG_PATHS = builtin_catalog_paths()
SDKM_TARGET = "JETSON_ORIN_NX_TARGETS"

def _query_output(display_label: str, version: str, install_method: str | None) -> str:
    method = f" --install-method {install_method}" if install_method else ""
    return (
        f"{display_label}\n"
        "sdkmanager --cli --action install --product Jetson "
        f"--version {version} --target {SDKM_TARGET}{method}\n"
    )


class FakeSdkManagerClient:
    def __init__(self, *, primary: str, current: str, archived: str) -> None:
        self.primary = primary
        self.current = current
        self.archived = archived
        self.query_calls: list[tuple[bool, bool]] = []

    def version(self) -> str:
        return "2.4.1.13536"

    def query_jetson(
        self, *, archived: bool = False, primary_only: bool = False
    ) -> str:
        self.query_calls.append((archived, primary_only))
        if archived:
            return self.archived
        return self.primary if primary_only else self.current


def _resolver() -> TargetResolver:
    return TargetResolver(CATALOG_PATHS.targets_dir, CATALOG_PATHS.schema_path)


@pytest.mark.parametrize(
    "selector,query_source,display_label,install_method",
    [
        ("jetson-orin@jp6.0", "archived", "JetPack 6.0 (rev. 2)", None),
        ("jetson-orin@jp6.1", "current-all", "JetPack 6.1 (rev. 1)", None),
        ("jetson-orin@jp6.2", "current-all", "JetPack 6.2 (rev. 2)", None),
        ("jetson-orin@jp6.2.1", "current", "JetPack 6.2.1 (rev. 1)", None),
        ("jetson-orin@jp6.2.2", "current", "JetPack 6.2.2", None),
        ("jetson-orin@jp6.2.3", "current", "JetPack 6.2.3", "direct_flash"),
    ],
)
def test_every_ga_target_has_exact_discovery_and_read_only_response_planning(
    selector: str,
    query_source: str,
    display_label: str,
    install_method: str | None,
    tmp_path: Path,
) -> None:
    target = _resolver().resolve(selector)
    output = _query_output(display_label, target.jetpack_version, install_method)
    client = FakeSdkManagerClient(
        primary=output if query_source == "current" else "",
        current=output if query_source == "current-all" else "",
        archived=output if query_source == "archived" else "",
    )

    discovery = discover_catalog_target(
        client,
        target,
        required_sdk_manager_target=target.sdk_manager_target,
    )
    role = sdk_manager_component_role(target.sdk_manager_component_role)
    data_root = tmp_path / "must-not-be-created"
    download_folder = data_root / "sdkm" / "downloads"
    response = render_response_file(
        discovery,
        role,
        download_folder=download_folder,
    )

    assert discovery.query_source == query_source
    assert discovery.target.canonical_id == target.canonical_id
    assert discovery.target.jetpack_version == target.jetpack_version
    assert discovery.target.sdk_manager_display_label == display_label
    assert discovery.target.sdk_manager_target == SDKM_TARGET
    expected_calls = {
        "current": [(False, True)],
        "current-all": [(False, True), (False, False)],
        "archived": [(False, True), (False, False), (True, False)],
    }
    assert client.query_calls == expected_calls[query_source]

    assert f"version = {target.jetpack_version}" in response
    assert f"target = {SDKM_TARGET}" in response
    assert role.install_method == install_method
    if install_method is None:
        assert not any(
            line.startswith("install-method =") for line in response.splitlines()
        )
    else:
        assert f"install-method = {install_method}" in response
    assert not data_root.exists()
