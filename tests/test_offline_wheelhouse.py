from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "release" / "build_offline_wheelhouse.py"

spec = importlib.util.spec_from_file_location("build_offline_wheelhouse", TOOL)
assert spec is not None and spec.loader is not None
wheelhouse = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wheelhouse
spec.loader.exec_module(wheelhouse)


def _wheel(
    directory: Path,
    name: str,
    version: str,
    *,
    python_tag: str = "py3",
    abi_tag: str = "none",
    platform_tag: str = "any",
    build_tag: str | None = None,
    orin: bool = False,
) -> Path:
    filename_name = name.replace("-", "_")
    prefix = f"{filename_name}-{version}"
    if build_tag is not None:
        prefix += f"-{build_tag}"
    path = directory / f"{prefix}-{python_tag}-{abi_tag}-{platform_tag}.whl"
    dist_info = f"{filename_name}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            f"Wheel-Version: 1.0\nTag: {python_tag}-{abi_tag}-{platform_tag}\n",
        )
        if orin:
            archive.writestr("orin_stage/__init__.py", '__version__ = "0.1.0"\n')
            archive.writestr(
                "orin_stage/catalog/data/schema/target.schema.json", "{}\n"
            )
            for index in range(7):
                archive.writestr(
                    f"orin_stage/catalog/data/targets/target-{index}.yaml", "---\n"
                )
            for index in range(2):
                archive.writestr(
                    f"orin_stage/catalog/data/hardware/hardware-{index}.yaml", "---\n"
                )
            archive.writestr(f"{dist_info}/licenses/LICENSE", "MIT\n")
            archive.writestr(
                f"{dist_info}/entry_points.txt",
                "[console_scripts]\nostg = orin_stage.cli:main\n",
            )
    return path


def _locked_wheels(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    build = wheelhouse.read_exact_lock(wheelhouse.BUILD_LOCK)
    runtime = wheelhouse.read_exact_lock(wheelhouse.RUNTIME_LOCK)
    (root / "build").mkdir(parents=True)
    (root / "runtime").mkdir()
    for name, version in build.items():
        native = name == "maturin"
        _wheel(
            root / "build",
            name,
            version,
            python_tag="cp310" if native else "py3",
            abi_tag="cp310" if native else "none",
            platform_tag="linux_x86_64" if native else "any",
        )
    for name, version in runtime.items():
        native = name in wheelhouse.NATIVE_RUNTIME
        _wheel(
            root / "runtime",
            name,
            version,
            python_tag="cp310" if native else "py3",
            abi_tag="cp310" if native else "none",
            platform_tag="linux_x86_64" if native else "any",
        )
    _wheel(root / "runtime", "orin-stage", "0.1.0", orin=True)
    return build, runtime


def test_exact_build_and_runtime_wheel_sets_match_locks(tmp_path: Path) -> None:
    build, runtime = _locked_wheels(tmp_path)
    build_wheels = wheelhouse.validate_wheel_set(tmp_path / "build", build, "build")
    runtime["orin-stage"] = "0.1.0"
    runtime_wheels = wheelhouse.validate_wheel_set(
        tmp_path / "runtime", runtime, "runtime"
    )
    assert len(build_wheels) == 15
    assert len(runtime_wheels) == 8
    assert not (
        {wheel.name for wheel in runtime_wheels} & wheelhouse.BUILD_ONLY
    )


def test_duplicate_package_version_is_rejected(tmp_path: Path) -> None:
    _wheel(tmp_path, "example", "1.0")
    _wheel(tmp_path, "example", "1.0", build_tag="1")
    with pytest.raises(wheelhouse.WheelhouseError, match="duplicate"):
        wheelhouse.validate_wheel_set(tmp_path, {"example": "1.0"}, "build")


def test_build_only_package_cannot_leak_into_runtime(tmp_path: Path) -> None:
    _wheel(tmp_path, "hatchling", "1.27.0")
    with pytest.raises(wheelhouse.WheelhouseError, match="build-only"):
        wheelhouse.validate_wheel_set(
            tmp_path, {"hatchling": "1.27.0"}, "runtime"
        )


@pytest.mark.parametrize("name", ["pyyaml", "rpds-py"])
def test_native_runtime_package_cannot_be_pure(tmp_path: Path, name: str) -> None:
    _wheel(tmp_path, name, "1.0")
    with pytest.raises(wheelhouse.WheelhouseError, match="pure wheel"):
        wheelhouse.validate_wheel_set(tmp_path, {name: "1.0"}, "runtime")


def test_orin_wheel_requires_catalog_license_and_entry_point(tmp_path: Path) -> None:
    valid = _wheel(tmp_path, "orin-stage", "0.1.0", orin=True)
    wheelhouse.verify_orin_wheel(valid)
    invalid = _wheel(tmp_path, "orin-stage", "0.1.0", build_tag="1")
    with pytest.raises(wheelhouse.WheelhouseError, match="package or catalog"):
        wheelhouse.verify_orin_wheel(invalid)


def test_wheel_manifest_hashes_and_exact_sets_are_verified(tmp_path: Path) -> None:
    build, runtime = _locked_wheels(tmp_path)
    runtime["orin-stage"] = "0.1.0"
    wheels = (
        *wheelhouse.validate_wheel_set(tmp_path / "build", build, "build"),
        *wheelhouse.validate_wheel_set(tmp_path / "runtime", runtime, "runtime"),
    )
    wheelhouse._write_manifest(tmp_path / "WHEELS.json", "jammy", wheels)
    assert len(wheelhouse.verify_wheel_manifest(tmp_path, "jammy")) == 23

    payload = json.loads((tmp_path / "WHEELS.json").read_text(encoding="utf-8"))
    payload["wheels"][0]["sha256"] = hashlib.sha256(b"wrong").hexdigest()
    (tmp_path / "WHEELS.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(wheelhouse.WheelhouseError, match="manifest mismatch"):
        wheelhouse.verify_wheel_manifest(tmp_path, "jammy")


def test_locked_source_inputs_are_not_mutated() -> None:
    before = wheelhouse.snapshot_locks()
    assert before == wheelhouse.snapshot_locks()
    assert set(before) == {
        str(path.relative_to(REPO_ROOT)) for path in wheelhouse.LOCKED_INPUTS
    }


def test_bootstrap_order_covers_hidden_runtime_import_edges() -> None:
    order = {name: index for index, name in enumerate(wheelhouse.BUILD_ORDER)}
    assert set(order) == set(wheelhouse.read_exact_lock(wheelhouse.BUILD_LOCK))
    assert order["flit-core"] < order["packaging"] < order["setuptools"]
    assert order["pathspec"] < order["hatchling"]
    assert order["pluggy"] < order["hatchling"]
    assert order["trove-classifiers"] < order["hatchling"]
    assert order["setuptools-rust"] < order["maturin"]


def test_wheel_inspection_ignores_nested_vendored_metadata(tmp_path: Path) -> None:
    path = _wheel(tmp_path, "example", "1.0")
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(
            "example/vendor/helper-2.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: helper\nVersion: 2.0\n",
        )
    inspected = wheelhouse.inspect_wheel(path, "build")
    assert (inspected.name, inspected.version) == ("example", "1.0")
