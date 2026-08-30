from __future__ import annotations

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_INPUT = REPO_ROOT / "release" / "dependencies" / "runtime.in"
RUNTIME_LOCK = REPO_ROOT / "release" / "dependencies" / "runtime.lock"
EXPECTED_RUNTIME_PACKAGES = {
    "attrs",
    "jsonschema",
    "jsonschema-specifications",
    "pyyaml",
    "referencing",
    "rpds-py",
    "typing-extensions",
}


def _requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _project_dependencies() -> list[str]:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"(?m)^dependencies\s*=\s*(\[[^]]*\])", pyproject)
    assert match is not None
    dependencies = ast.literal_eval(match.group(1))
    assert isinstance(dependencies, list)
    return dependencies


def test_runtime_input_matches_project_dependencies() -> None:
    assert _requirement_lines(RUNTIME_INPUT) == _project_dependencies()


def test_runtime_lock_is_sorted_unique_exact_closure() -> None:
    requirements = _requirement_lines(RUNTIME_LOCK)
    assert requirements == sorted(
        requirements,
        key=lambda requirement: requirement.partition("==")[0],
    )

    names: list[str] = []
    for requirement in requirements:
        match = re.fullmatch(
            r"([a-z0-9]+(?:-[a-z0-9]+)*)==([a-zA-Z0-9][a-zA-Z0-9.!+_-]*)",
            requirement,
        )
        assert match is not None, f"runtime dependency is not exactly pinned: {requirement}"
        names.append(match.group(1))

    assert len(names) == len(set(names))
    assert set(names) == EXPECTED_RUNTIME_PACKAGES
