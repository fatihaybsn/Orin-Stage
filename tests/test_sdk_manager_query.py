from __future__ import annotations

import pytest

from orin_stage.acquisition.sdk_manager_query import (
    SdkManagerJetsonRelease,
    SdkManagerQueryParseError,
    find_jetpack_release,
    parse_jetson_query_output,
)


def test_parse_realistic_jp623_query_extracts_exact_orin_targets() -> None:
    raw = """
+++++++++++++++++++++++++++++++++++++++++++++++++++
+   Welcome to NVIDIA SDK MANAGER v2.4.1.13536   +
+++++++++++++++++++++++++++++++++++++++++++++++++++
Available options are:
JetPack 6.2.3
sdkmanager --cli --action install --login-type devzone --product Jetson --version 6.2.3 --target-os Linux --host --target JETSON_AGX_ORIN_TARGETS --flash --install-method direct_flash --additional-sdk 'DeepStream 7.1' --additional-sdk 'Holoscan 4.4'
sdkmanager --cli --action install --login-type devzone --product Jetson --version 6.2.3 --target-os Linux --host --target JETSON_ORIN_NX_TARGETS --flash --install-method direct_flash --additional-sdk 'DeepStream 7.1' --additional-sdk 'Holoscan 4.4'
sdkmanager --cli --action install --login-type devzone --product Jetson --version 6.2.3 --target-os Linux --host --target JETSON_ORIN_NANO_TARGETS --flash --install-method direct_flash --additional-sdk 'DeepStream 7.1' --additional-sdk 'Holoscan 4.4'
Query completed.
"""

    assert parse_jetson_query_output(raw) == (
        SdkManagerJetsonRelease(
            display_label="JetPack 6.2.3",
            version="6.2.3",
            targets=(
                "JETSON_AGX_ORIN_TARGETS",
                "JETSON_ORIN_NX_TARGETS",
                "JETSON_ORIN_NANO_TARGETS",
            ),
        ),
    )


def test_parser_preserves_human_revision_label_but_uses_cli_version() -> None:
    raw = """
JetPack 6.2.1 (rev. 1)
sdkmanager --cli --action install --login-type devzone --product Jetson --version 6.2.1 --target-os Linux --host --target JETSON_ORIN_NX_TARGETS --flash
"""

    release = parse_jetson_query_output(raw)[0]

    assert release.display_label == "JetPack 6.2.1 (rev. 1)"
    assert release.version == "6.2.1"


def test_archived_jp60_output_is_parsed_without_special_case() -> None:
    raw = """
Available options are:
JetPack 6.0 (rev. 2)
sdkmanager --cli --action install --login-type devzone --product Jetson --version 6.0 --target-os Linux --host --target JETSON_AGX_ORIN_TARGETS --flash
sdkmanager --cli --action install --login-type devzone --product Jetson --version 6.0 --target-os Linux --host --target JETSON_ORIN_NX_TARGETS --flash
sdkmanager --cli --action install --login-type devzone --product Jetson --version 6.0 --target-os Linux --host --target JETSON_ORIN_NANO_TARGETS --flash
Query completed.
"""

    release = parse_jetson_query_output(raw)[0]

    assert release.version == "6.0"
    assert release.targets == (
        "JETSON_AGX_ORIN_TARGETS",
        "JETSON_ORIN_NX_TARGETS",
        "JETSON_ORIN_NANO_TARGETS",
    )


def test_duplicate_target_commands_are_collapsed() -> None:
    raw = """
JetPack 7.2.1
sdkmanager --cli --action install --product Jetson --version 7.2.1 --target JETSON_ORIN_NX_TARGETS --install-method direct_flash
sdkmanager --cli --action install --product Jetson --version 7.2.1 --target JETSON_ORIN_NX_TARGETS --install-method iso_flash
"""

    release = parse_jetson_query_output(raw)[0]

    assert release.targets == ("JETSON_ORIN_NX_TARGETS",)


def test_parser_rejects_conflicting_versions_inside_one_jetpack_entry() -> None:
    raw = """
JetPack 6.2.3
sdkmanager --cli --action install --product Jetson --version 6.2.3 --target JETSON_ORIN_NX_TARGETS
sdkmanager --cli --action install --product Jetson --version 6.2.2 --target JETSON_ORIN_NANO_TARGETS
"""

    with pytest.raises(SdkManagerQueryParseError, match="Conflicting --version"):
        parse_jetson_query_output(raw)


def test_find_jetpack_release_uses_exact_version_only() -> None:
    releases = (
        SdkManagerJetsonRelease("JetPack 6.2", "6.2", ("A",)),
        SdkManagerJetsonRelease("JetPack 6.2.3", "6.2.3", ("B",)),
    )

    assert find_jetpack_release(releases, "6.2").version == "6.2"

    with pytest.raises(LookupError, match="6.2.1"):
        find_jetpack_release(releases, "6.2.1")
