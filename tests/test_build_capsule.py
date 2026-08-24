from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orin_stage.build_capsule import (
    BuildCapsuleError,
    BuildCapsuleNotFoundError,
    BuildCapsuleRunner,
    BuildCommandError,
)


def _directories(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace" / "root"
    repository = tmp_path / "repository"
    toolchain = tmp_path / "toolchain"
    workspace.mkdir(parents=True)
    repository.mkdir()
    toolchain.mkdir()
    return workspace, repository, toolchain


def test_run_mounts_same_target_read_only_and_repository_read_write(
    tmp_path: Path,
) -> None:
    workspace, repository, toolchain = _directories(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    completed = BuildCapsuleRunner().run(
        workspace,
        repository,
        toolchain,
        ("/bin/true",),
        runner=runner,
    )

    assert completed.stdout == "ok\n"
    command, kwargs = calls[0]
    assert command == (
        "podman",
        "run",
        "--rm",
        "--volume",
        f"{workspace.resolve()}:/target:ro",
        "--volume",
        f"{toolchain.resolve()}:/opt/toolchain:ro",
        "--volume",
        f"{repository.resolve()}:/workspace:rw",
        "--workdir",
        "/workspace",
        "docker.io/library/ubuntu:22.04",
        "/bin/true",
    )
    assert kwargs == {
        "check": False,
        "text": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }


def test_custom_image_is_used(tmp_path: Path) -> None:
    workspace, repository, toolchain = _directories(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(command, **kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    BuildCapsuleRunner(image="example/build@sha256:abc").run(
        workspace,
        repository,
        toolchain,
        ("make",),
        runner=runner,
    )

    assert "example/build@sha256:abc" in calls[0]


def test_all_input_directories_must_exist(tmp_path: Path) -> None:
    workspace, repository, toolchain = _directories(tmp_path)

    with pytest.raises(BuildCapsuleError, match="workspace root"):
        BuildCapsuleRunner().run(
            tmp_path / "missing-workspace",
            repository,
            toolchain,
            ("/bin/true",),
        )

    with pytest.raises(BuildCapsuleError, match="repository root"):
        BuildCapsuleRunner().run(
            workspace,
            tmp_path / "missing-repository",
            toolchain,
            ("/bin/true",),
        )

    with pytest.raises(BuildCapsuleError, match="toolchain root"):
        BuildCapsuleRunner().run(
            workspace,
            repository,
            tmp_path / "missing-toolchain",
            ("/bin/true",),
        )


def test_build_command_must_not_be_empty(tmp_path: Path) -> None:
    workspace, repository, toolchain = _directories(tmp_path)

    with pytest.raises(BuildCapsuleError, match="must not be empty"):
        BuildCapsuleRunner().run(workspace, repository, toolchain, ())


def test_build_command_arguments_must_be_strings(tmp_path: Path) -> None:
    workspace, repository, toolchain = _directories(tmp_path)

    with pytest.raises(BuildCapsuleError, match="must be strings"):
        BuildCapsuleRunner().run(
            workspace,
            repository,
            toolchain,
            ("echo", 123),  # type: ignore[arg-type]
        )


def test_missing_podman_has_specific_error(tmp_path: Path) -> None:
    workspace, repository, toolchain = _directories(tmp_path)

    def runner(command, **kwargs):
        raise FileNotFoundError(command[0])

    with pytest.raises(BuildCapsuleNotFoundError, match="Podman executable"):
        BuildCapsuleRunner(podman_binary="missing-podman").run(
            workspace,
            repository,
            toolchain,
            ("/bin/true",),
            runner=runner,
        )


def test_failed_build_preserves_process_evidence(tmp_path: Path) -> None:
    workspace, repository, toolchain = _directories(tmp_path)

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            23,
            stdout="partial build\n",
            stderr="build failed\n",
        )

    with pytest.raises(BuildCommandError) as captured:
        BuildCapsuleRunner().run(
            workspace,
            repository,
            toolchain,
            ("make",),
            runner=runner,
        )

    error = captured.value
    assert error.returncode == 23
    assert error.stdout == "partial build\n"
    assert error.stderr == "build failed\n"
    assert error.command[-1] == "make"
