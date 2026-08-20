from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orin_stage.base.sandbox import (
    HOST_BUILDER_IMAGE,
    ConstructionSandboxError,
    HostConstructionSandbox,
)


def _l4t_tree(tmp_path: Path) -> Path:
    root = tmp_path / "Linux_for_Tegra"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "l4t_flash_prerequisites.sh").write_text("#!/bin/sh\n")
    (root / "apply_binaries.sh").write_text("#!/bin/sh\n")
    return root


def test_host_construction_sandbox_runs_official_scripts_in_one_disposable_container(
    tmp_path: Path,
) -> None:
    root = _l4t_tree(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(command, **kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    HostConstructionSandbox().run_official_l4t_scripts(root, runner=runner)

    assert len(calls) == 1
    command = calls[0]
    assert command[:2] == ("podman", "run")
    assert "--rm" in command
    assert "--privileged" in command
    assert HOST_BUILDER_IMAGE in command
    assert f"{root.resolve()}:/work/Linux_for_Tegra:rw" in command
    shell_script = command[-1]
    assert shell_script.index("l4t_flash_prerequisites.sh") < shell_script.index(
        "apply_binaries.sh"
    )


def test_host_construction_sandbox_requires_official_scripts(tmp_path: Path) -> None:
    root = tmp_path / "Linux_for_Tegra"
    root.mkdir()

    with pytest.raises(ConstructionSandboxError, match="script is missing"):
        HostConstructionSandbox().run_official_l4t_scripts(root)
