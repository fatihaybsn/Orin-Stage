from __future__ import annotations

from pathlib import Path

import pytest

from orin_stage.acquisition.sdk_manager_discovery import SdkManagerDiscovery
from orin_stage.acquisition.sdk_manager_match import VerifiedSdkManagerTarget
from orin_stage.acquisition.sdk_manager_response import (
    SdkManagerResponseFileError,
    render_response_file,
    response_file_sha256,
    write_response_file_atomic,
)
from orin_stage.acquisition.sdk_manager_role import (
    JP6_DEVELOPER_ROLE_V1,
    SdkManagerComponentRole,
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


def test_jp6_developer_role_is_explicit_and_versioned() -> None:
    role = JP6_DEVELOPER_ROLE_V1

    assert role.role_id == "jp6-developer-v1"
    assert role.include_host is True
    assert role.select_groups == (
        "Jetson Linux",
        "Jetson Runtime Components",
        "Jetson SDK Components",
    )
    assert role.additional_sdks == ()
    assert len(role.digest()) == 64


def test_role_digest_changes_when_selection_changes() -> None:
    changed = SdkManagerComponentRole(
        role_id="jp6-developer-v1",
        include_host=False,
        select_groups=JP6_DEVELOPER_ROLE_V1.select_groups,
    )
    assert changed.digest() != JP6_DEVELOPER_ROLE_V1.digest()


def test_response_file_is_minimal_and_does_not_accept_license_or_flash() -> None:
    text = render_response_file(
        _discovery(),
        JP6_DEVELOPER_ROLE_V1,
        download_folder=Path("/srv/orin-stage/sdkm/downloads"),
    )

    assert "action = downloadonly" in text
    assert "version = 6.2.3" in text
    assert "target = JETSON_ORIN_NX_TARGETS" in text
    assert "host = true" in text
    assert text.count("select[] = ") == 3
    assert "flash =" not in text
    assert "license =" not in text
    assert "sudo-password" not in text
    assert "additional-sdk" not in text


def test_response_file_rejects_relative_download_root() -> None:
    with pytest.raises(SdkManagerResponseFileError, match="absolute"):
        render_response_file(
            _discovery(),
            JP6_DEVELOPER_ROLE_V1,
            download_folder=Path("sdkm/downloads"),
        )


def test_response_file_is_written_atomically_with_matching_digest(tmp_path: Path) -> None:
    path = tmp_path / "sdkm" / "responses" / "jp623.ini"
    text = render_response_file(
        _discovery(),
        JP6_DEVELOPER_ROLE_V1,
        download_folder=tmp_path.resolve() / "sdkm" / "downloads",
    )

    response = write_response_file_atomic(
        path.resolve(), text, role=JP6_DEVELOPER_ROLE_V1
    )

    assert response.path.read_text(encoding="utf-8") == text
    assert response.sha256 == response_file_sha256(text)
    assert response.role_digest == JP6_DEVELOPER_ROLE_V1.digest()
