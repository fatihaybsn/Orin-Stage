from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from orin_stage import __version__
from orin_stage.build_toolchain import BuildToolchainManager


PROCESS_TIMEOUT_SECONDS = 10


def _ostg_executable() -> Path:
    executable = Path(sys.executable).parent / "ostg"
    assert executable.is_file(), (
        f"installed ostg console script not found next to test interpreter: {executable}"
    )
    assert os.access(executable, os.X_OK), (
        f"installed ostg console script is not executable: {executable}"
    )
    return executable


def _run_ostg(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    outside_repository = tmp_path / "outside-repo"
    outside_repository.mkdir(exist_ok=True)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment["HOME"] = str(isolated_home)

    return subprocess.run(
        (_ostg_executable(), *arguments),
        cwd=outside_repository,
        env=environment,
        check=False,
        shell=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=PROCESS_TIMEOUT_SECONDS,
    )


def test_no_command_prints_help_and_exits_zero(tmp_path: Path) -> None:
    completed = _run_ostg(tmp_path)

    assert completed.returncode == 0
    assert "usage: ostg" in completed.stdout
    assert "Orin Stage" in completed.stdout
    assert completed.stderr == ""
    assert "Traceback" not in completed.stdout


def test_explicit_help_lists_current_command_surface(tmp_path: Path) -> None:
    completed = _run_ostg(tmp_path, "--help")

    assert completed.returncode == 0
    assert "usage: ostg" in completed.stdout
    for command in (
        "doctor",
        "target",
        "workspace",
        "shell",
        "run",
        "build",
        "inspect",
        "storage",
    ):
        assert command in completed.stdout
    assert completed.stderr == ""


def test_version_uses_installed_package_version(tmp_path: Path) -> None:
    completed = _run_ostg(tmp_path, "--version")

    assert completed.returncode == 0
    assert completed.stdout.strip() == f"ostg {__version__}"
    assert completed.stderr == ""


def test_invalid_subcommand_is_argparse_exit_two(tmp_path: Path) -> None:
    completed = _run_ostg(tmp_path, "definitely-not-a-command")

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "usage: ostg" in completed.stderr
    assert "error:" in completed.stderr
    assert "definitely-not-a-command" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_target_list_loads_packaged_catalog_outside_repository(tmp_path: Path) -> None:
    completed = _run_ostg(tmp_path, "target", "list")

    assert completed.returncode == 0
    assert "jetson-orin@jp6.2.3" in completed.stdout
    assert "6.2.3" in completed.stdout
    assert "validation-pending" in completed.stdout
    assert completed.stderr == ""


def test_empty_workspace_list_is_read_only_for_missing_data_root(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data-root"

    completed = _run_ostg(
        tmp_path,
        "--data-root",
        str(data_root),
        "workspace",
        "list",
    )

    assert completed.returncode == 0
    lines = completed.stdout.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].split() == ["NAME", "ID", "JETPACK", "GENERATION"]
    assert completed.stderr == ""
    assert "Traceback" not in completed.stdout
    assert not data_root.exists()


def test_inspect_missing_workspace_is_short_domain_error(tmp_path: Path) -> None:
    data_root = tmp_path / "data-root"
    data_root.mkdir()

    completed = _run_ostg(
        tmp_path,
        "--data-root",
        str(data_root),
        "inspect",
        "--workspace",
        "missing",
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "workspace" in completed.stderr.lower()
    assert "missing" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_run_missing_workspace_fails_before_podman(tmp_path: Path) -> None:
    data_root = tmp_path / "data-root"
    data_root.mkdir()

    completed = _run_ostg(
        tmp_path,
        "--data-root",
        str(data_root),
        "run",
        "--workspace",
        "missing",
        "--",
        "/bin/true",
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "workspace" in completed.stderr.lower()
    assert "missing" in completed.stderr
    assert "Podman" not in completed.stderr
    assert "Traceback" not in completed.stderr


def test_build_missing_workspace_creates_no_toolchain_state(tmp_path: Path) -> None:
    data_root = tmp_path / "data-root"
    data_root.mkdir()
    toolchain = BuildToolchainManager(data_root)

    completed = _run_ostg(
        tmp_path,
        "--data-root",
        str(data_root),
        "build",
        "--workspace",
        "missing",
        "--",
        "/bin/true",
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "workspace" in completed.stderr.lower()
    assert "missing" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert not toolchain.toolchain_path.exists()
    assert not toolchain.toolchains_dir.exists()
    assert tuple(data_root.iterdir()) == ()
