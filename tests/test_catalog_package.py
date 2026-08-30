from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("catalog-wheel")
    project = root / "project"
    project.mkdir()
    shutil.copy2(REPO_ROOT / "pyproject.toml", project / "pyproject.toml")
    shutil.copy2(REPO_ROOT / "README.md", project / "README.md")
    shutil.copy2(REPO_ROOT / "LICENSE", project / "LICENSE")
    shutil.copytree(REPO_ROOT / "src", project / "src")
    wheel_dir = root / "wheel"
    wheel_dir.mkdir()
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            str(project),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    wheels = list(wheel_dir.glob("orin_stage-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_wheel_contains_builtin_catalog_schema_targets_and_hardware(
    built_wheel: Path,
) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())

    assert "orin_stage/catalog/data/schema/target.schema.json" in names
    assert len(
        {
            name
            for name in names
            if name.startswith("orin_stage/catalog/data/targets/")
            and name.endswith(".yaml")
        }
    ) == 7
    assert len(
        {
            name
            for name in names
            if name.startswith("orin_stage/catalog/data/hardware/")
            and name.endswith(".yaml")
        }
    ) == 2
    assert not any(name.startswith("catalog/") for name in names)


def test_wheel_declares_mit_and_contains_license(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        license_name = next(
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/licenses/LICENSE")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
        license_text = archive.read(license_name).decode("utf-8")

    assert "License-Expression: MIT\n" in metadata
    assert "License-File: LICENSE\n" in metadata
    assert license_text == (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")


def test_installed_builtin_resolver_works_outside_repository(
    built_wheel: Path,
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed),
            str(built_wheel),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    outside_repository = tmp_path / "outside-repository"
    outside_repository.mkdir()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(installed)
    environment["PYTHONNOUSERSITE"] = "1"
    script = """
from orin_stage.catalog import TargetResolver, builtin_catalog_paths
paths = builtin_catalog_paths()
resolver = TargetResolver(paths.targets_dir, paths.schema_path)
targets = resolver.list_targets()
assert len(targets) == 6
assert all(target.support_status == "validation-pending" for target in targets)
print(paths.targets_dir)
"""

    probe = subprocess.run(
        (sys.executable, "-c", script),
        cwd=outside_repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert probe.returncode == 0, probe.stderr
    assert str(installed) in probe.stdout
    assert str(REPO_ROOT / "catalog") not in probe.stdout
