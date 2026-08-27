from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from orin_stage.catalog import (
    CatalogSemanticValidationError,
    TargetResolver,
    builtin_catalog_paths,
)
from orin_stage.catalog.semantic import validate_target_semantics


CATALOG_PATHS = builtin_catalog_paths()
TARGETS_DIR = CATALOG_PATHS.targets_dir
SCHEMA_PATH = CATALOG_PATHS.schema_path


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict)
    return data


def write_yaml(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)


def copy_catalog(tmp_path: Path) -> Path:
    target_copy = tmp_path / "targets"
    shutil.copytree(TARGETS_DIR, target_copy)
    return target_copy


def assert_semantic_error(tmp_path: Path, mutate, expected_fragment: str) -> None:
    targets = copy_catalog(tmp_path)
    path = targets / "jp6.2.3.yaml"
    record = load_yaml(path)
    mutate(record)
    write_yaml(path, record)

    with pytest.raises(CatalogSemanticValidationError) as exc_info:
        TargetResolver(targets, SCHEMA_PATH)

    assert expected_fragment in str(exc_info.value)


def test_every_checked_in_target_passes_semantic_validation() -> None:
    for path in sorted(TARGETS_DIR.glob("*.yaml")):
        record = load_yaml(path)
        assert validate_target_semantics(record) == (), path.name


def test_filename_case_variants_are_intentionally_accepted() -> None:
    # Several NVIDIA sources preserve r/R literal differences. They identify
    # the same artifact and must not be collapsed in the catalog.
    record = load_yaml(TARGETS_DIR / "jp6.2.yaml")
    assert (
        record["construction_inputs"]["bsp"]["filename"]
        != record["checksums"]["official"]["artifacts"]["bsp"]["filename"]
    )
    assert validate_target_semantics(record) == ()


def test_hardware_profile_files_are_not_dereferenced_at_this_stage() -> None:
    # Hardware YAML content is intentionally deferred. Target semantic
    # validation only validates the profile identifiers carried by the record.
    record = load_yaml(TARGETS_DIR / "jp6.2.3.yaml")
    assert validate_target_semantics(record) == ()


def test_canonical_id_must_match_release_identity(tmp_path: Path) -> None:
    assert_semantic_error(
        tmp_path,
        lambda r: r.__setitem__("id", "nvidia.jetpack-6.2.3.jetson-linux-36.5.1"),
        "id: must match release identity",
    )


def test_primary_alias_must_be_first_and_match_jetpack_identity(tmp_path: Path) -> None:
    assert_semantic_error(
        tmp_path,
        lambda r: r.__setitem__(
            "aliases", ["jetson-orin@jp6.2.2", "jetson-orin@jp6.2.3"]
        ),
        "aliases[0]: must be the primary exact selector 'jetson-orin@jp6.2.3'",
    )


def test_l4t_must_match_jetson_linux_release_revision(tmp_path: Path) -> None:
    assert_semantic_error(
        tmp_path,
        lambda r: r["release"]["l4t"].__setitem__("version", "36.5.1"),
        "release.l4t.version",
    )


def test_release_tag_must_follow_display_version(tmp_path: Path) -> None:
    assert_semantic_error(
        tmp_path,
        lambda r: r["release"]["jetson_linux"].__setitem__("release_tag", "jetson_36.5"),
        "release.jetson_linux.release_tag",
    )


@pytest.mark.parametrize("artifact_name", ["bsp", "sample_rootfs"])
def test_construction_input_release_must_match_artifact_revision(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    assert_semantic_error(
        tmp_path,
        lambda r: r["construction_inputs"][artifact_name].__setitem__("release", "36.5.1"),
        f"construction_inputs.{artifact_name}.release",
    )


def test_repository_suites_must_match_release_channel(tmp_path: Path) -> None:
    assert_semantic_error(
        tmp_path,
        lambda r: r["packages"]["repository"].__setitem__(
            "suites", ["common r36.4 main", "t234 r36.4 main"]
        ),
        "packages.repository.suites",
    )


def test_checksum_must_still_identify_same_artifact(tmp_path: Path) -> None:
    assert_semantic_error(
        tmp_path,
        lambda r: r["checksums"]["official"]["artifacts"]["bsp"].__setitem__(
            "filename", "some_other_bsp.tbz2"
        ),
        "checksums.official.artifacts.bsp.filename",
    )


def test_checksum_reference_and_checksum_source_must_agree(tmp_path: Path) -> None:
    assert_semantic_error(
        tmp_path,
        lambda r: r["sources"]["references"].__setitem__(
            "release_checksums", "https://developer.nvidia.com/not-the-checksum-source.txt"
        ),
        "sources.references.release_checksums",
    )


def test_evidence_field_must_reference_an_existing_catalog_field(tmp_path: Path) -> None:
    def mutate(record: dict) -> None:
        record["sources"]["evidence"][0]["field"] = "userspace.kernel.nonexistent"

    assert_semantic_error(
        tmp_path,
        mutate,
        "references unknown catalog field 'userspace.kernel.nonexistent'",
    )
