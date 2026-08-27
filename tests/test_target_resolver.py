from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from orin_stage.catalog import builtin_catalog_paths
from orin_stage.catalog.resolver import (
    CatalogTargetValidationError,
    DuplicateSelectorError,
    DuplicateTargetIdError,
    TargetNotFoundError,
    TargetResolver,
)


CATALOG_PATHS = builtin_catalog_paths()
TARGETS_DIR = CATALOG_PATHS.targets_dir
SCHEMA_PATH = CATALOG_PATHS.schema_path

PRODUCTION_ALIASES = {
    "jetson-orin@jp6.0": "nvidia.jetpack-6.0.jetson-linux-36.3",
    "jetson-orin@jp6.1": "nvidia.jetpack-6.1.jetson-linux-36.4",
    "jetson-orin@jp6.2": "nvidia.jetpack-6.2.jetson-linux-36.4.3",
    "jetson-orin@jp6.2.1": "nvidia.jetpack-6.2.1.jetson-linux-36.4.4",
    "jetson-orin@jp6.2.2": "nvidia.jetpack-6.2.2.jetson-linux-36.5.0",
    "jetson-orin@jp6.2.3": "nvidia.jetpack-6.2.3.jetson-linux-36.5.2",
}


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


@pytest.fixture(scope="session")
def resolver() -> TargetResolver:
    return TargetResolver(TARGETS_DIR, SCHEMA_PATH)


@pytest.mark.parametrize("alias,canonical_id", PRODUCTION_ALIASES.items())
def test_every_production_alias_resolves_exactly(
    resolver: TargetResolver,
    alias: str,
    canonical_id: str,
) -> None:
    resolved = resolver.resolve(alias)
    assert resolved.selector == alias
    assert resolved.canonical_id == canonical_id
    assert resolved.support_status == "validation-pending"


def test_canonical_id_and_alias_resolve_same_record(resolver: TargetResolver) -> None:
    alias = "jetson-orin@jp6.2.3"
    by_alias = resolver.resolve(alias)
    by_id = resolver.resolve(by_alias.canonical_id)

    assert by_alias.canonical_id == by_id.canonical_id
    assert by_alias.record == by_id.record
    assert by_alias.source_path == by_id.source_path


def test_developer_preview_reference_preserves_unavailable_status(
    resolver: TargetResolver,
) -> None:
    resolved = resolver.resolve("jetson-orin@jp6.0-dp")
    assert resolved.canonical_id == "nvidia.jetpack-6.0-dp.jetson-linux-36.2"
    assert resolved.is_unavailable
    assert not resolved.is_supported


def test_unknown_selector_is_rejected_without_guessing(resolver: TargetResolver) -> None:
    with pytest.raises(TargetNotFoundError):
        resolver.resolve("jetson-orin@jp6.2.4")


@pytest.mark.parametrize(
    "selector",
    [
        "JETSON-ORIN@JP6.2.3",
        "jp6.2.3",
        "jetson-orin@6.2.3",
        "jetson-orin@jp6.2",
    ],
)
def test_resolver_does_not_invent_aliases_or_prefix_match(
    resolver: TargetResolver,
    selector: str,
) -> None:
    if selector == "jetson-orin@jp6.2":
        # This is an explicitly declared exact alias and therefore valid; it
        # must resolve JP6.2, never be treated as a prefix for JP6.2.x.
        resolved = resolver.resolve(selector)
        assert resolved.canonical_id == "nvidia.jetpack-6.2.jetson-linux-36.4.3"
        return

    with pytest.raises(TargetNotFoundError):
        resolver.resolve(selector)


def test_resolved_record_is_a_copy_and_cannot_mutate_resolver_state(
    resolver: TargetResolver,
) -> None:
    first = resolver.resolve("jetson-orin@jp6.2.3")
    first.record["support"]["status"] = "supported"

    second = resolver.resolve("jetson-orin@jp6.2.3")
    assert second.support_status == "validation-pending"
    assert second.record["support"]["status"] == "validation-pending"


def test_selector_namespace_contains_canonical_ids_and_declared_aliases(
    resolver: TargetResolver,
) -> None:
    selectors = set(resolver.selectors())
    canonical_ids = set(resolver.canonical_ids())

    assert len(canonical_ids) == 7
    assert canonical_ids <= selectors
    assert set(PRODUCTION_ALIASES) <= selectors
    assert "jetson-orin@jp6.0-dp" in selectors


def test_duplicate_canonical_id_is_rejected(tmp_path: Path) -> None:
    targets = copy_catalog(tmp_path)
    source = load_yaml(targets / "jp6.2.3.yaml")
    duplicate = copy.deepcopy(source)
    write_yaml(targets / "duplicate-id.yaml", duplicate)

    with pytest.raises(DuplicateTargetIdError):
        TargetResolver(targets, SCHEMA_PATH)


def test_duplicate_alias_is_rejected(tmp_path: Path) -> None:
    targets = copy_catalog(tmp_path)
    target = load_yaml(targets / "jp6.2.2.yaml")
    target["aliases"] = ["jetson-orin@jp6.2.2", "jetson-orin@jp6.2.3"]
    write_yaml(targets / "jp6.2.2.yaml", target)

    with pytest.raises(DuplicateSelectorError):
        TargetResolver(targets, SCHEMA_PATH)


def test_schema_invalid_target_prevents_catalog_publication(tmp_path: Path) -> None:
    targets = copy_catalog(tmp_path)
    target = load_yaml(targets / "jp6.2.3.yaml")
    target["support"]["status"] = "ready"
    write_yaml(targets / "jp6.2.3.yaml", target)

    with pytest.raises(CatalogTargetValidationError):
        TargetResolver(targets, SCHEMA_PATH)
