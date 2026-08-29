from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orin_stage.cli import build_parser, main
from orin_stage.target_executor import TargetCommandError
from orin_stage.workspace_manager import WorkspaceManager


WORKSPACE_ID = "0123456789abcdef0123456789abcdef"
TARGET_LOCK_DIGEST = "a" * 64
BASE_DIGEST = "b" * 64


def _normal_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orin_stage.cli.os.geteuid", lambda: 1000)


def _workspace(tmp_path: Path, *, generation: int = 0) -> Path:
    data_root = tmp_path / "data"
    workspace = data_root / "workspaces" / WORKSPACE_ID
    (workspace / "root").mkdir(parents=True)
    (workspace / "workspace.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "workspace_id": WORKSPACE_ID,
                "workspace_name": "demo",
                "target_lock_digest": TARGET_LOCK_DIGEST,
                "base_digest": BASE_DIGEST,
                "generation": generation,
            }
        ),
        encoding="utf-8",
    )
    return data_root


def test_top_level_help_contains_shell_and_run(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["--help"])

    assert captured.value.code == 0
    output = capsys.readouterr().out
    assert "shell" in output
    assert "run" in output


def test_shell_passes_exact_workspace_selector_to_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = tmp_path / "data"
    calls: list[str] = []

    class FakeManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root

        def shell(self, selector: str) -> subprocess.CompletedProcess[str]:
            calls.append(selector)
            return subprocess.CompletedProcess(("/bin/bash",), 0)

    monkeypatch.setattr("orin_stage.cli.WorkspaceManager", FakeManager)

    assert main(["--data-root", str(data_root), "shell", "--workspace", "demo"]) == 0
    assert calls == ["demo"]


def test_shell_clean_exit_advances_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = _workspace(tmp_path, generation=2)

    class SuccessfulExecutor:
        def __init__(self, *, podman_binary: str) -> None:
            assert podman_binary == "podman"

        def shell(self, root: Path, *, runner: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(("/bin/bash",), 0)

    monkeypatch.setattr("orin_stage.workspace_manager.TargetExecutor", SuccessfulExecutor)

    assert main(["--data-root", str(data_root), "shell", "--workspace", "demo"]) == 0
    assert WorkspaceManager(data_root).open("demo").generation == 3


def test_shell_failure_does_not_advance_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = _workspace(tmp_path, generation=4)

    class FailingExecutor:
        def __init__(self, *, podman_binary: str) -> None:
            pass

        def shell(self, root: Path, *, runner: object) -> object:
            raise TargetCommandError(("podman", "run", "/bin/bash"), 17, None, None)

    monkeypatch.setattr("orin_stage.workspace_manager.TargetExecutor", FailingExecutor)

    assert main(["--data-root", str(data_root), "shell", "--workspace", "demo"]) == 17
    assert WorkspaceManager(data_root).open("demo").generation == 4


def test_run_preserves_target_argv_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = tmp_path / "data"
    calls: list[tuple[str, tuple[str, ...]]] = []

    class FakeManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root

        def run(
            self,
            selector: str,
            command: tuple[str, ...],
        ) -> subprocess.CompletedProcess[str]:
            calls.append((selector, command))
            return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("orin_stage.cli.WorkspaceManager", FakeManager)
    argv = ("sh", "-c", "echo hello > /tmp/test", "", "two words")

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "run",
                "--workspace",
                "demo",
                "--",
                *argv,
            ]
        )
        == 0
    )
    assert calls == [("demo", argv)]


def test_run_forwards_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = tmp_path / "data"

    class FakeManager:
        def __init__(self, selected_root: Path) -> None:
            pass

        def run(self, selector: str, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, "target out\n", "target err\n")

    monkeypatch.setattr("orin_stage.cli.WorkspaceManager", FakeManager)

    assert main(["--data-root", str(data_root), "run", "--workspace", "demo", "--", "true"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "target out\n"
    assert captured.err == "target err\n"


def test_run_success_advances_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = _workspace(tmp_path, generation=6)

    class SuccessfulExecutor:
        def __init__(self, *, podman_binary: str) -> None:
            pass

        def run(
            self,
            root: Path,
            command: tuple[str, ...],
            *,
            runner: object,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr("orin_stage.workspace_manager.TargetExecutor", SuccessfulExecutor)

    assert main(["--data-root", str(data_root), "run", "--workspace", "demo", "--", "true"]) == 0
    assert WorkspaceManager(data_root).open("demo").generation == 7


def test_run_nonzero_forwards_evidence_and_keeps_generation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = _workspace(tmp_path, generation=8)

    class FailingExecutor:
        def __init__(self, *, podman_binary: str) -> None:
            pass

        def run(self, root: Path, command: tuple[str, ...], *, runner: object) -> object:
            raise TargetCommandError(
                ("podman", "run", "--rootfs", str(root), *command),
                42,
                "partial out\n",
                "target failed\n",
            )

    monkeypatch.setattr("orin_stage.workspace_manager.TargetExecutor", FailingExecutor)

    assert main(["--data-root", str(data_root), "run", "--workspace", "demo", "--", "false"]) == 42
    captured = capsys.readouterr()
    assert captured.out == "partial out\n"
    assert captured.err == "target failed\n"
    assert "Traceback" not in captured.err
    assert WorkspaceManager(data_root).open("demo").generation == 8


def test_run_unknown_workspace_returns_one_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = tmp_path / "data"
    data_root.mkdir()

    assert main(["--data-root", str(data_root), "run", "--workspace", "missing", "--", "true"]) == 1
    error = capsys.readouterr().err
    assert error == "error: workspace not found: missing\n"
    assert "Traceback" not in error


@pytest.mark.parametrize(
    ("arguments", "operation"),
    [
        (["shell", "--workspace", "demo"], "shell"),
        (["run", "--workspace", "demo", "--", "true"], "run"),
    ],
)
def test_shell_and_run_reject_root_invocation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    operation: str,
) -> None:
    monkeypatch.setattr("orin_stage.cli.os.geteuid", lambda: 0)

    class ForbiddenManager:
        def __init__(self, selected_root: Path) -> None:
            raise AssertionError("manager must not be constructed as root")

    monkeypatch.setattr("orin_stage.cli.WorkspaceManager", ForbiddenManager)

    assert main(arguments) == 1
    assert capsys.readouterr().err == f"error: Run ostg {operation} as your normal user.\n"


def test_run_without_target_command_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        main(["run", "--workspace", "demo", "--"])

    assert captured.value.code == 2
    assert "requires a command after '--'" in capsys.readouterr().err
