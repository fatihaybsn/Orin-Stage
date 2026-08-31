from __future__ import annotations

import ast
import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_LOCK = REPO_ROOT / "release" / "dependencies" / "build-tools.lock"
BUILD_SOURCES = (
    REPO_ROOT / "release" / "dependencies" / "build-sources.lock.json"
)
RUNTIME_LOCK = REPO_ROOT / "release" / "dependencies" / "runtime.lock"
PYPROJECT = REPO_ROOT / "pyproject.toml"

EXPECTED_BUILD_TOOLS = {
    "calver": "2025.3.31",
    "flit-core": "3.11.0",
    "hatch-fancy-pypi-readme": "23.2.0",
    "hatch-vcs": "0.5.0",
    "hatchling": "1.27.0",
    "maturin": "1.9.0",
    "packaging": "24.2",
    "pathspec": "0.12.1",
    "pluggy": "1.5.0",
    "semantic-version": "2.10.0",
    "setuptools": "77.0.3",
    "setuptools-rust": "1.11.1",
    "setuptools-scm": "8.2.0",
    "tomli": "2.0.2",
    "trove-classifiers": "2025.5.9.12",
}


def _requirement_versions(path: Path) -> dict[str, str]:
    requirements = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert requirements == sorted(
        requirements, key=lambda requirement: requirement.partition("==")[0]
    )

    versions: dict[str, str] = {}
    for requirement in requirements:
        match = re.fullmatch(
            r"([a-z0-9]+(?:-[a-z0-9]+)*)==([a-zA-Z0-9][a-zA-Z0-9.!+_-]*)",
            requirement,
        )
        assert match is not None, f"build tool is not exactly pinned: {requirement}"
        name, version = match.groups()
        assert name not in versions, f"duplicate build tool: {name}"
        versions[name] = version
    return versions


def _manifest_sources() -> list[dict[str, object]]:
    manifest = json.loads(BUILD_SOURCES.read_text(encoding="utf-8"))
    assert manifest.keys() == {"schema_version", "sources"}
    assert manifest["schema_version"] == 1
    sources = manifest["sources"]
    assert isinstance(sources, list)
    return sources


def _supports(requires_python: object, target: tuple[int, int]) -> bool:
    if requires_python is None:
        return True
    assert isinstance(requires_python, str)
    match = re.fullmatch(r">=(\d+)\.(\d+)", requires_python)
    assert match is not None, f"unsupported Requires-Python test input: {requires_python}"
    minimum = (int(match.group(1)), int(match.group(2)))
    return target >= minimum


def test_build_tools_lock_is_sorted_unique_exact_closure() -> None:
    assert _requirement_versions(BUILD_LOCK) == EXPECTED_BUILD_TOOLS


def test_build_sources_match_build_tools_one_to_one() -> None:
    sources = _manifest_sources()
    names = [entry["name"] for entry in sources]
    assert names == sorted(names)
    assert len(names) == len(set(names))
    assert {entry["name"]: entry["version"] for entry in sources} == (
        EXPECTED_BUILD_TOOLS
    )


def test_build_sources_are_hashed_https_sdists() -> None:
    for entry in _manifest_sources():
        filename = entry["filename"]
        url = entry["url"]
        sha256 = entry["sha256"]
        assert isinstance(filename, str)
        assert filename.endswith((".tar.gz", ".tar.bz2", ".tar.xz"))
        assert not filename.endswith(".whl")
        assert isinstance(url, str)
        assert url.startswith("https://files.pythonhosted.org/")
        assert url.endswith(f"/{filename}")
        assert isinstance(sha256, str)
        assert re.fullmatch(r"[0-9a-f]{64}", sha256)
        assert isinstance(entry["size"], int) and entry["size"] > 0


def test_build_sources_support_python_310() -> None:
    incompatible = [
        entry["name"]
        for entry in _manifest_sources()
        if not _supports(entry["requires_python"], (3, 10))
    ]
    assert incompatible == []


def test_build_sources_support_python_312() -> None:
    incompatible = [
        entry["name"]
        for entry in _manifest_sources()
        if not _supports(entry["requires_python"], (3, 12))
    ]
    assert incompatible == []


def test_build_only_packages_do_not_leak_into_runtime_lock() -> None:
    runtime_names = set(_requirement_versions(RUNTIME_LOCK))
    assert runtime_names.isdisjoint(EXPECTED_BUILD_TOOLS)


def test_production_dependency_and_build_contract_is_unchanged() -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    dependencies_match = re.search(
        r"(?m)^dependencies\s*=\s*(\[[^]]*\])", pyproject
    )
    build_requires_match = re.search(
        r"(?ms)^\[build-system\]\s*requires\s*=\s*(\[[^]]*\])",
        pyproject,
    )
    assert dependencies_match is not None
    assert build_requires_match is not None
    assert ast.literal_eval(dependencies_match.group(1)) == [
        "PyYAML>=6,<7",
        "jsonschema>=4.23,<5",
    ]
    assert ast.literal_eval(build_requires_match.group(1)) == ["setuptools>=77"]
