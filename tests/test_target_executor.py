from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orin_stage.target_executor import (
    TargetCommandError,
    TargetExecutor,
    TargetExecutorError,
    TargetExecutorNotFoundError,
)


def _workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace" / "root"
    root.mkdir(parents=True)
    return root


def test_run_uses_workspace_as_external_rootfs_and_captures_output(
    tmp_path: Path,
) -> None:
    root = _workspace_root(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="arm64\n", stderr="")

    completed = TargetExecutor().run(
        root,
        ("dpkg", "--print-architecture"),
        runner=runner,
    )

    assert completed.stdout == "arm64\n"
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == (
        "podman",
        "run",
        "--rm",
        "--rootfs",
        str(root.resolve()),
        "dpkg",
        "--print-architecture",
    )
    assert ":O" not in command
    assert kwargs == {
        "check": False,
        "text": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }


def test_shell_attaches_terminal_without_capturing_stdio(tmp_path: Path) -> None:
    root = _workspace_root(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(command, 0)

    TargetExecutor().shell(root, runner=runner)

    command, kwargs = calls[0]
    assert command == (
        "podman",
        "run",
        "--rm",
        "--interactive",
        "--tty",
        "--rootfs",
        str(root.resolve()),
        "/bin/bash",
    )
    assert kwargs == {"check": False, "text": True}


def test_workspace_root_must_exist(tmp_path: Path) -> None:
    with pytest.raises(TargetExecutorError, match="existing directory"):
        TargetExecutor().run(tmp_path / "missing", ("/bin/true",))


def test_target_command_must_not_be_empty(tmp_path: Path) -> None:
    root = _workspace_root(tmp_path)

    with pytest.raises(TargetExecutorError, match="must not be empty"):
        TargetExecutor().run(root, ())


def test_target_command_arguments_must_be_strings(tmp_path: Path) -> None:
    root = _workspace_root(tmp_path)

    with pytest.raises(TargetExecutorError, match="must be strings"):
        TargetExecutor().run(root, ("echo", 123))  # type: ignore[arg-type]


def test_missing_podman_has_specific_error(tmp_path: Path) -> None:
    root = _workspace_root(tmp_path)

    def runner(command, **kwargs):
        raise FileNotFoundError(command[0])

    with pytest.raises(TargetExecutorNotFoundError, match="Podman executable"):
        TargetExecutor(podman_binary="missing-podman").run(
            root,
            ("/bin/true",),
            runner=runner,
        )


def test_failed_target_command_preserves_process_evidence(tmp_path: Path) -> None:
    root = _workspace_root(tmp_path)

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            42,
            stdout="partial output\n",
            stderr="target failure\n",
        )

    with pytest.raises(TargetCommandError) as captured:
        TargetExecutor().run(root, ("/bin/false",), runner=runner)

    error = captured.value
    assert error.returncode == 42
    assert error.stdout == "partial output\n"
    assert error.stderr == "target failure\n"
    assert error.command[-1] == "/bin/false"
