from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orin_stage.target_corpus import TargetCorpusError, run_arm64_userspace_corpus
from orin_stage.target_executor import TargetCommandError


class FakeExecutor:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def run(self, workspace_root: Path, command: tuple[str, ...]):
        self.calls.append((workspace_root, command))
        key = command[0]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=self.outputs[key],
            stderr="",
        )


def _successful_outputs() -> dict[str, str]:
    return {
        "/lib/ld-linux-aarch64.so.1": (
            "libc.so.6 => /lib/aarch64-linux-gnu/libc.so.6\n"
            "/lib/ld-linux-aarch64.so.1\n"
        ),
        "/bin/bash": "BASH_OK\n",
        "/usr/bin/python3": (
            "a010cbd21e9d9c398048a2758e24e61a1ba133e0a9adcbf4ab03f6d3aa8c2a51\n"
        ),
        "/usr/bin/dpkg": "arm64\n",
        "/usr/bin/apt-cache": (
            "nvidia-jetpack:\n"
            "  Installed: 6.2.3+b81\n"
            "  Candidate: 6.2.3+b81\n"
        ),
    }


def test_corpus_runs_only_the_minimum_userspace_checks(tmp_path: Path) -> None:
    executor = FakeExecutor(_successful_outputs())

    results = run_arm64_userspace_corpus(tmp_path, executor=executor)  # type: ignore[arg-type]

    assert [result.name for result in results] == [
        "loader",
        "bash",
        "python_cpu",
        "dpkg",
        "apt",
    ]
    assert len(executor.calls) == 5
    assert executor.calls[-1][1] == (
        "/usr/bin/apt-cache",
        "policy",
        "nvidia-jetpack",
    )


def test_corpus_rejects_non_arm64_dpkg_result(tmp_path: Path) -> None:
    outputs = _successful_outputs()
    outputs["/usr/bin/dpkg"] = "amd64\n"

    with pytest.raises(TargetCorpusError, match="dpkg"):
        run_arm64_userspace_corpus(
            tmp_path,
            executor=FakeExecutor(outputs),  # type: ignore[arg-type]
        )


def test_corpus_rejects_wrong_cpu_result(tmp_path: Path) -> None:
    outputs = _successful_outputs()
    outputs["/usr/bin/python3"] = "wrong\n"

    with pytest.raises(TargetCorpusError, match="python_cpu"):
        run_arm64_userspace_corpus(
            tmp_path,
            executor=FakeExecutor(outputs),  # type: ignore[arg-type]
        )


def test_corpus_requires_installed_jetpack_metadata(tmp_path: Path) -> None:
    outputs = _successful_outputs()
    outputs["/usr/bin/apt-cache"] = "nvidia-jetpack:\n  Installed: (none)\n"

    with pytest.raises(TargetCorpusError, match="apt"):
        run_arm64_userspace_corpus(
            tmp_path,
            executor=FakeExecutor(outputs),  # type: ignore[arg-type]
        )


def test_corpus_wraps_target_command_failure(tmp_path: Path) -> None:
    class FailingExecutor:
        def run(self, workspace_root: Path, command: tuple[str, ...]):
            raise TargetCommandError(
                command=("podman", "run", *command),
                returncode=7,
                stdout="partial\n",
                stderr="failed\n",
            )

    with pytest.raises(TargetCorpusError, match="could not run") as captured:
        run_arm64_userspace_corpus(
            tmp_path,
            executor=FailingExecutor(),  # type: ignore[arg-type]
        )

    assert isinstance(captured.value.__cause__, TargetCommandError)
