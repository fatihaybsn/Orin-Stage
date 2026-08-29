from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orin_stage.build_capsule import BuildCommandError
from orin_stage.build_toolchain import BuildToolchainError
from orin_stage.cli import build_parser, main
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


def _fake_toolchain_manager(
    monkeypatch: pytest.MonkeyPatch,
    data_root: Path,
    toolchain_root: Path,
    *,
    reused: bool,
) -> list[Path]:
    calls: list[Path] = []

    class FakeToolchainManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root

        def ensure(self) -> object:
            calls.append(data_root)
            return SimpleNamespace(root_path=toolchain_root, reused=reused)

    monkeypatch.setattr("orin_stage.cli.BuildToolchainManager", FakeToolchainManager)
    return calls


def test_top_level_help_contains_build(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["--help"])

    assert captured.value.code == 0
    assert "build" in capsys.readouterr().out


def test_build_help_describes_workspace_command_without_manual_roots(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["build", "--help"])

    assert captured.value.code == 0
    output = capsys.readouterr().out
    assert "--workspace WORKSPACE" in output
    assert "COMMAND" in output
    assert "--repository" not in output
    assert "--toolchain-root" not in output


def test_build_without_command_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        main(["build", "--workspace", "demo", "--"])

    assert captured.value.code == 2
    assert "ostg build requires a command after '--'" in capsys.readouterr().err


def test_build_forwards_selector_cwd_toolchain_and_argv_exactly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = tmp_path / "data"
    repository = tmp_path / "repository"
    toolchain = tmp_path / "managed" / "root"
    repository.mkdir()
    toolchain.mkdir(parents=True)
    monkeypatch.chdir(repository)
    ensure_calls = _fake_toolchain_manager(
        monkeypatch,
        data_root,
        toolchain,
        reused=True,
    )
    build_calls: list[tuple[str, Path, Path, tuple[str, ...]]] = []

    class FakeWorkspaceManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root

        def build(
            self,
            selector: str,
            repository_root: Path,
            toolchain_root: Path,
            command: tuple[str, ...],
        ) -> subprocess.CompletedProcess[str]:
            build_calls.append(
                (selector, repository_root, toolchain_root, command)
            )
            return subprocess.CompletedProcess(
                command,
                0,
                "build out\n",
                "build err\n",
            )

    monkeypatch.setattr("orin_stage.cli.WorkspaceManager", FakeWorkspaceManager)
    argv = ("sh", "-c", "printf '%s\\n' 'two words'", "", "literal value")

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "build",
                "--workspace",
                "jp623-demo",
                "--",
                *argv,
            ]
        )
        == 0
    )
    assert ensure_calls == [data_root]
    assert build_calls == [("jp623-demo", Path.cwd(), toolchain, argv)]
    captured = capsys.readouterr()
    assert captured.out == "build out\n"
    assert captured.err == "build err\n"


def test_missing_managed_toolchain_uses_existing_ensure_acquisition_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = tmp_path / "data"
    repository = tmp_path / "repository"
    toolchain = tmp_path / "managed" / "root"
    repository.mkdir()
    toolchain.mkdir(parents=True)
    monkeypatch.chdir(repository)
    ensure_calls = _fake_toolchain_manager(
        monkeypatch,
        data_root,
        toolchain,
        reused=False,
    )

    class FakeWorkspaceManager:
        def __init__(self, selected_root: Path) -> None:
            pass

        def build(
            self,
            selector: str,
            repository_root: Path,
            toolchain_root: Path,
            command: tuple[str, ...],
        ) -> subprocess.CompletedProcess[str]:
            assert toolchain_root == toolchain
            return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("orin_stage.cli.WorkspaceManager", FakeWorkspaceManager)

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "build",
                "--workspace",
                "demo",
                "--",
                "make",
            ]
        )
        == 0
    )
    assert ensure_calls == [data_root]


def test_successful_build_keeps_generation_and_uses_workspace_manager_capsule_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = _workspace(tmp_path, generation=7)
    repository = tmp_path / "repository"
    toolchain = tmp_path / "managed" / "root"
    repository.mkdir()
    toolchain.mkdir(parents=True)
    monkeypatch.chdir(repository)
    _fake_toolchain_manager(monkeypatch, data_root, toolchain, reused=True)
    capsule_calls: list[tuple[Path, Path, Path, tuple[str, ...]]] = []

    class SuccessfulCapsule:
        def __init__(self, *, podman_binary: str) -> None:
            assert podman_binary == "podman"

        def run(
            self,
            workspace_root: Path,
            repository_root: Path,
            toolchain_root: Path,
            command: tuple[str, ...],
            *,
            runner: object,
        ) -> subprocess.CompletedProcess[str]:
            capsule_calls.append(
                (workspace_root, repository_root, toolchain_root, command)
            )
            return subprocess.CompletedProcess(command, 0, "built\n", "warning\n")

    monkeypatch.setattr(
        "orin_stage.workspace_manager.BuildCapsuleRunner",
        SuccessfulCapsule,
    )

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "build",
                "--workspace",
                "demo",
                "--",
                "make",
                "all",
            ]
        )
        == 0
    )
    workspace = WorkspaceManager(data_root).open("demo")
    assert workspace.generation == 7
    assert capsule_calls == [
        (workspace.root_path, repository, toolchain, ("make", "all"))
    ]
    captured = capsys.readouterr()
    assert captured.out == "built\n"
    assert captured.err == "warning\n"


def test_failed_build_returns_real_code_preserves_output_and_generation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = _workspace(tmp_path, generation=9)
    repository = tmp_path / "repository"
    toolchain = tmp_path / "managed" / "root"
    repository.mkdir()
    toolchain.mkdir(parents=True)
    monkeypatch.chdir(repository)
    _fake_toolchain_manager(monkeypatch, data_root, toolchain, reused=True)

    class FailingCapsule:
        def __init__(self, *, podman_binary: str) -> None:
            pass

        def run(
            self,
            workspace_root: Path,
            repository_root: Path,
            toolchain_root: Path,
            command: tuple[str, ...],
            *,
            runner: object,
        ) -> object:
            raise BuildCommandError(
                ("podman", "run", *command),
                42,
                "partial build\n",
                "build failed\n",
            )

    monkeypatch.setattr(
        "orin_stage.workspace_manager.BuildCapsuleRunner",
        FailingCapsule,
    )

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "build",
                "--workspace",
                "demo",
                "--",
                "false",
            ]
        )
        == 42
    )
    captured = capsys.readouterr()
    assert captured.out == "partial build\n"
    assert captured.err == "build failed\n"
    assert "Traceback" not in captured.err
    assert WorkspaceManager(data_root).open("demo").generation == 9


def test_build_unknown_workspace_returns_one_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = tmp_path / "data"
    data_root.mkdir()
    toolchain = tmp_path / "managed" / "root"
    toolchain.mkdir(parents=True)
    _fake_toolchain_manager(monkeypatch, data_root, toolchain, reused=True)

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "build",
                "--workspace",
                "missing",
                "--",
                "true",
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert error == "error: workspace not found: missing\n"
    assert "Traceback" not in error


def test_toolchain_acquisition_failure_returns_one_without_build(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _normal_user(monkeypatch)
    data_root = tmp_path / "data"

    class FailingToolchainManager:
        def __init__(self, selected_root: Path) -> None:
            assert selected_root == data_root

        def ensure(self) -> object:
            raise BuildToolchainError("managed toolchain SHA-256 mismatch")

    class ForbiddenWorkspaceManager:
        def __init__(self, selected_root: Path) -> None:
            raise AssertionError("build must not start after toolchain failure")

    monkeypatch.setattr(
        "orin_stage.cli.BuildToolchainManager",
        FailingToolchainManager,
    )
    monkeypatch.setattr(
        "orin_stage.cli.WorkspaceManager",
        ForbiddenWorkspaceManager,
    )

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "build",
                "--workspace",
                "demo",
                "--",
                "make",
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert error == "error: managed toolchain SHA-256 mismatch\n"
    assert "Traceback" not in error


def test_build_rejects_root_before_toolchain_or_workspace_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("orin_stage.cli.os.geteuid", lambda: 0)

    class ForbiddenManager:
        def __init__(self, selected_root: Path) -> None:
            raise AssertionError("build dependencies must not run as root")

    monkeypatch.setattr("orin_stage.cli.BuildToolchainManager", ForbiddenManager)
    monkeypatch.setattr("orin_stage.cli.WorkspaceManager", ForbiddenManager)

    assert main(["build", "--workspace", "demo", "--", "make"]) == 1
    assert capsys.readouterr().err == (
        "error: Run ostg build as your normal user.\n"
    )
