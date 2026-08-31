from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "release" / "stage_private_runtime.py"


def _module():
    spec = importlib.util.spec_from_file_location("private_runtime_stage_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_lock_is_exact_and_excludes_build_only_packages() -> None:
    stage = _module()
    locked = stage.read_exact_lock(stage.RUNTIME_LOCK)

    assert locked == {
        "attrs": "26.1.0",
        "jsonschema": "4.26.0",
        "jsonschema-specifications": "2025.9.1",
        "pyyaml": "6.0.3",
        "referencing": "0.37.0",
        "rpds-py": "0.30.0",
        "typing-extensions": "4.16.0",
    }
    assert not set(locked) & stage.BUILD_ONLY


def test_launcher_contract_uses_isolated_private_interpreter_and_preserves_argv() -> None:
    stage = _module()
    production_python = Path("/usr/lib/orin-stage/venv/bin/python")

    assert stage.launcher_text(production_python) == (
        "#!/bin/sh\n"
        'exec "/usr/lib/orin-stage/venv/bin/python" -I -m orin_stage.cli "$@"\n'
    )


def test_runtime_distribution_validation_accepts_only_runtime_and_bootstrap() -> None:
    stage = _module()
    installed = stage.read_exact_lock(stage.RUNTIME_LOCK)
    installed.update({"orin-stage": "0.1.0", "pip": "24.0", "setuptools": "68.0"})

    stage.validate_installed_distributions(installed)


@pytest.mark.parametrize("extra", ("hatchling", "maturin", "unexpected-package"))
def test_runtime_distribution_validation_rejects_build_or_unexpected_packages(extra: str) -> None:
    stage = _module()
    installed = stage.read_exact_lock(stage.RUNTIME_LOCK)
    installed["orin-stage"] = "0.1.0"
    installed[extra] = "1.0"

    with pytest.raises(stage.RuntimeStageError):
        stage.validate_installed_distributions(installed)


def test_permission_validation_rejects_world_writable_runtime_tree(tmp_path: Path) -> None:
    stage = _module()
    launcher = tmp_path / "usr" / "bin" / "ostg"
    runtime = tmp_path / "usr" / "lib" / "orin-stage" / "venv"
    launcher.parent.mkdir(parents=True)
    runtime.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    for path in (
        tmp_path,
        tmp_path / "usr",
        tmp_path / "usr" / "lib",
        launcher.parent,
        runtime.parent,
        runtime,
        launcher,
    ):
        path.chmod(0o755)
    owner = os.getuid()

    stage.validate_tree_permissions(tmp_path, owner_uid=owner)
    runtime.chmod(0o777)
    with pytest.raises(stage.RuntimeStageError, match="writable"):
        stage.validate_tree_permissions(tmp_path, owner_uid=owner)
