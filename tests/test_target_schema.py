from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "catalog" / "schema" / "target.schema.json"
TARGETS_DIR = REPO_ROOT / "catalog" / "targets"
REFERENCE_TARGET = TARGETS_DIR / "jp6.2.3.yaml"
DP_TARGET = TARGETS_DIR / "jp6.0-dp.reference.yaml"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    assert isinstance(data, dict), f"{path} must contain a YAML mapping at the document root"
    return data


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    return load_json(SCHEMA_PATH)


@pytest.fixture(scope="session")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.fixture
def valid_target() -> dict[str, Any]:
    return load_yaml(REFERENCE_TARGET)


def assert_invalid(validator: Draft202012Validator, instance: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        validator.validate(instance)


def set_bad_sha1(target: dict[str, Any]) -> None:
    target["checksums"]["official"]["artifacts"]["bsp"]["digest"] = "not-a-valid-sha1"


def remove_id(target: dict[str, Any]) -> None:
    del target["id"]


def set_invalid_support_status(target: dict[str, Any]) -> None:
    target["support"]["status"] = "ready"


def add_typo_property(target: dict[str, Any]) -> None:
    target["release_revison"] = "36.5.2"


def set_wrong_target_abi(target: dict[str, Any]) -> None:
    target["userspace"]["architecture"]["target_abi"] = "x86_64"


def set_wrong_debian_architecture(target: dict[str, Any]) -> None:
    target["userspace"]["architecture"]["debian_architecture"] = "amd64"


def add_runtime_sha256_to_catalog(target: dict[str, Any]) -> None:
    target["checksums"]["official"]["artifacts"]["bsp"]["sha256"] = "0" * 64


def break_fixed_meta_package_contract(target: dict[str, Any]) -> None:
    meta = target["packages"]["meta_package"]
    meta["version_build"] = None


def break_dlfw_contract(target: dict[str, Any]) -> None:
    del target["nvidia_stack"]["dlfw"]["note"]


NEGATIVE_MUTATIONS: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("invalid SHA-1", set_bad_sha1),
    ("missing id", remove_id),
    ("invalid support.status", set_invalid_support_status),
    ("unknown/typo property", add_typo_property),
    ("wrong target ABI", set_wrong_target_abi),
    ("wrong Debian architecture", set_wrong_debian_architecture),
    ("runtime SHA-256 leaked into catalog", add_runtime_sha256_to_catalog),
    ("broken meta-package conditional", break_fixed_meta_package_contract),
    ("broken DLFW conditional", break_dlfw_contract),
]


def test_schema_is_valid_draft_2020_12(schema: dict[str, Any]) -> None:
    """The schema document itself must be a valid Draft 2020-12 schema."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:  # pragma: no cover - keeps failure output explicit
        pytest.fail(f"Invalid target.schema.json: {exc}")


def target_files() -> list[Path]:
    return sorted(TARGETS_DIR.glob("*.yaml"))


@pytest.mark.parametrize("target_path", target_files(), ids=lambda path: path.name)
def test_every_catalog_target_matches_schema(
    validator: Draft202012Validator,
    target_path: Path,
) -> None:
    """Every checked-in JP6 target record must satisfy target.schema.json."""
    target = load_yaml(target_path)

    errors = sorted(validator.iter_errors(target), key=lambda error: list(error.absolute_path))
    if errors:
        details = "\n".join(
            f"- {'/'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
            for error in errors
        )
        pytest.fail(f"{target_path.relative_to(REPO_ROOT)} failed schema validation:\n{details}")



@pytest.mark.parametrize("case_name,mutate", NEGATIVE_MUTATIONS, ids=[case[0] for case in NEGATIVE_MUTATIONS])
def test_invalid_catalog_records_are_rejected(
    validator: Draft202012Validator,
    valid_target: dict[str, Any],
    case_name: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """Known structural/catalog-contract violations must fail validation."""
    broken = copy.deepcopy(valid_target)
    mutate(broken)
    assert_invalid(validator, broken)


def test_developer_preview_cannot_be_validation_pending(
    validator: Draft202012Validator,
) -> None:
    """Developer Preview is a reference record and cannot enter the support gate."""
    target = load_yaml(DP_TARGET)
    target["support"]["status"] = "validation-pending"
    assert_invalid(validator, target)


def test_supported_status_is_schema_valid(
    validator: Draft202012Validator,
    valid_target: dict[str, Any],
) -> None:
    """Gate evidence is resolver/runtime policy, so schema must allow supported GA records."""
    target = copy.deepcopy(valid_target)
    target["support"]["status"] = "supported"
    validator.validate(target)
