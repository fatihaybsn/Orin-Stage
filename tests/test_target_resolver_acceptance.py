from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from orin_stage.catalog import (
    CatalogTargetValidationError,
    TargetNotUsableError,
    TargetResolver,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TARGETS_DIR = REPO_ROOT / "catalog" / "targets"
SCHEMA_PATH = REPO_ROOT / "catalog" / "schema" / "target.schema.json"


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


def test_target_list_exposes_only_ga_product_targets_without_promoting_any_release() -> None:
    resolver = TargetResolver(TARGETS_DIR, SCHEMA_PATH)

    targets = resolver.list_targets()

    assert len(targets) == 6
    assert all(t.availability == "ga" for t in targets)
    assert all(t.lifecycle == "production" for t in targets)
    assert sum(t.is_validation_pending for t in targets) == 6
    assert sum(t.is_unavailable for t in targets) == 0
    assert sum(t.is_supported for t in targets) == 0


def test_target_list_contains_expected_production_vertical_slice() -> None:
    resolver = TargetResolver(TARGETS_DIR, SCHEMA_PATH)

    target = next(
        item
        for item in resolver.list_targets()
        if item.primary_selector == "jetson-orin@jp6.2.3"
    )

    assert target.canonical_id == "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"
    assert target.jetpack_version == "6.2.3"
    assert target.jetson_linux_version == "36.5.2"
    assert target.lifecycle == "production"
    assert target.availability == "ga"
    assert target.support_status == "validation-pending"


def test_unavailable_ga_release_remains_visible_with_unavailable_status(tmp_path: Path) -> None:
    targets = copy_catalog(tmp_path)
    path = targets / "jp6.2.3.yaml"
    record = load_yaml(path)
    record["support"]["status"] = "unavailable"
    write_yaml(path, record)

    resolver = TargetResolver(targets, SCHEMA_PATH)
    target = next(
        item
        for item in resolver.list_targets()
        if item.primary_selector == "jetson-orin@jp6.2.3"
    )

    assert target.availability == "ga"
    assert target.lifecycle == "production"
    assert target.is_unavailable


def test_reference_dp_is_not_user_listed_but_remains_exactly_resolvable() -> None:
    resolver = TargetResolver(TARGETS_DIR, SCHEMA_PATH)

    assert all(
        item.primary_selector != "jetson-orin@jp6.0-dp"
        for item in resolver.list_targets()
    )

    dp = resolver.resolve("jetson-orin@jp6.0-dp")
    assert dp.record["release"]["jetpack"]["version"] == "6.0 DP"
    assert dp.record["release"]["jetpack"]["availability"] == "developer-preview"
    assert dp.record["release"]["jetpack"]["lifecycle"] == "pre-production"
    assert dp.support_status == "unavailable"


@pytest.mark.parametrize(
    "selector,expected_status",
    [
        ("jetson-orin@jp6.2.3", "validation-pending"),
        ("jetson-orin@jp6.0-dp", "unavailable"),
    ],
)
def test_resolve_for_use_rejects_every_non_supported_state(
    selector: str, expected_status: str
) -> None:
    resolver = TargetResolver(TARGETS_DIR, SCHEMA_PATH)

    with pytest.raises(TargetNotUsableError) as exc_info:
        resolver.resolve_for_use(selector)

    assert exc_info.value.selector == selector
    assert exc_info.value.support_status == expected_status


def test_resolve_for_use_accepts_only_explicit_supported_state(tmp_path: Path) -> None:
    targets = copy_catalog(tmp_path)
    path = targets / "jp6.2.3.yaml"
    record = load_yaml(path)
    record["support"]["status"] = "supported"
    write_yaml(path, record)

    resolver = TargetResolver(targets, SCHEMA_PATH)
    resolved = resolver.resolve_for_use("jetson-orin@jp6.2.3")

    assert resolved.is_supported
    assert resolved.canonical_id == "nvidia.jetpack-6.2.3.jetson-linux-36.5.2"


def test_identity_resolution_still_allows_inspecting_pending_target() -> None:
    resolver = TargetResolver(TARGETS_DIR, SCHEMA_PATH)

    resolved = resolver.resolve("jetson-orin@jp6.2.3")

    assert resolved.is_validation_pending
    assert not resolved.is_supported


def test_failed_reload_does_not_publish_partial_or_invalid_catalog(tmp_path: Path) -> None:
    targets = copy_catalog(tmp_path)
    resolver = TargetResolver(targets, SCHEMA_PATH)
    before_ids = resolver.canonical_ids()
    before_target = resolver.resolve("jetson-orin@jp6.2.3")

    broken_path = targets / "jp6.2.2.yaml"
    broken = load_yaml(broken_path)
    broken["support"]["status"] = "definitely-not-valid"
    write_yaml(broken_path, broken)

    with pytest.raises(CatalogTargetValidationError):
        resolver.reload()

    # reload() builds new indexes locally and publishes them only after the
    # entire catalog succeeds. A failed reload therefore leaves the previous
    # valid in-memory snapshot usable.
    assert resolver.canonical_ids() == before_ids
    after_target = resolver.resolve("jetson-orin@jp6.2.3")
    assert after_target.canonical_id == before_target.canonical_id
    assert after_target.support_status == before_target.support_status


def test_successful_reload_atomically_publishes_new_supported_state(tmp_path: Path) -> None:
    targets = copy_catalog(tmp_path)
    resolver = TargetResolver(targets, SCHEMA_PATH)
    assert resolver.resolve("jetson-orin@jp6.2.3").is_validation_pending

    path = targets / "jp6.2.3.yaml"
    record = load_yaml(path)
    record["support"]["status"] = "supported"
    write_yaml(path, record)

    resolver.reload()

    assert resolver.resolve("jetson-orin@jp6.2.3").is_supported
    assert resolver.resolve_for_use("jetson-orin@jp6.2.3").is_supported


def test_list_summaries_are_immutable_value_objects() -> None:
    resolver = TargetResolver(TARGETS_DIR, SCHEMA_PATH)
    summary = resolver.list_targets()[0]

    with pytest.raises((AttributeError, TypeError)):
        summary.support_status = "supported"  # type: ignore[misc]
