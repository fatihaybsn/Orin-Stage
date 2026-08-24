from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orin_stage.build_capsule import BuildCommandError
from orin_stage.build_corpus import (
    BuildCorpusError,
    JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_SHA256,
    run_same_tree_build_corpus,
    verify_toolchain_archive,
)
from orin_stage.target_executor import TargetCommandError


class FakeTargetExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def run(self, workspace_root: Path, command: tuple[str, ...]):
        self.calls.append((workspace_root, command))
        if self.fail:
            raise TargetCommandError(
                command=("podman", "run", *command),
                returncode=7,
                stdout="",
                stderr="target failed\n",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class FakeBuildRunner:
    def __init__(self, stdout: str, stderr: str = "", *, fail: bool = False) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.fail = fail
        self.calls: list[tuple[Path, Path, Path, tuple[str, ...]]] = []

    def run(
        self,
        workspace_root: Path,
        repository_root: Path,
        toolchain_root: Path,
        command: tuple[str, ...],
    ):
        self.calls.append((workspace_root, repository_root, toolchain_root, command))
        assert (repository_root / "hello.c").is_file()
        if self.fail:
            raise BuildCommandError(
                command=("podman", "run", *command),
                returncode=9,
                stdout="partial\n",
                stderr="build failed\n",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def _successful_stdout() -> str:
    return (
        "TOOLCHAIN_IDENTITY_OK\n"
        "SAME_TREE_OK\n"
        "TARGET_READ_ONLY_OK\n"
        "/target/usr/lib/aarch64-linux-gnu/Scrt1.o\n"
        "/opt/toolchain/lib/gcc/aarch64-buildroot-linux-gnu/11.3.0/libgcc.a\n"
        "  Machine:                           AArch64\n"
    )


def _directories(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace" / "root"
    toolchain = tmp_path / "toolchain"
    workspace.mkdir(parents=True)
    toolchain.mkdir()
    return workspace, toolchain


def test_corpus_runs_same_tree_read_only_arm64_build(tmp_path: Path) -> None:
    workspace, toolchain = _directories(tmp_path)
    target = FakeTargetExecutor()
    builder = FakeBuildRunner(_successful_stdout())

    result = run_same_tree_build_corpus(
        workspace,
        toolchain,
        build_runner=builder,  # type: ignore[arg-type]
        target_executor=target,  # type: ignore[arg-type]
    )

    assert "SAME_TREE_OK" in result.stdout
    assert len(target.calls) == 1
    assert "orin-stage-gate-e/marker.h" in target.calls[0][1][-1]
    assert len(builder.calls) == 1
    build_command = builder.calls[0][3]
    assert build_command[:2] == ("/bin/bash", "-lc")
    assert "--sysroot=/target" in build_command[2]
    assert "-B/target/usr/lib/aarch64-linux-gnu/" in build_command[2]


def test_corpus_rejects_writable_target(tmp_path: Path) -> None:
    workspace, toolchain = _directories(tmp_path)
    builder = FakeBuildRunner(
        "TOOLCHAIN_IDENTITY_OK\nSAME_TREE_OK\nTARGET_WRITABLE\n  Machine: AArch64\n"
    )

    with pytest.raises(BuildCorpusError, match="read-only"):
        run_same_tree_build_corpus(
            workspace,
            toolchain,
            build_runner=builder,  # type: ignore[arg-type]
            target_executor=FakeTargetExecutor(),  # type: ignore[arg-type]
        )


def test_corpus_rejects_non_aarch64_artifact(tmp_path: Path) -> None:
    workspace, toolchain = _directories(tmp_path)
    builder = FakeBuildRunner("TOOLCHAIN_IDENTITY_OK\nSAME_TREE_OK\nTARGET_READ_ONLY_OK\n  Machine: X86-64\n")

    with pytest.raises(BuildCorpusError, match="AArch64"):
        run_same_tree_build_corpus(
            workspace,
            toolchain,
            build_runner=builder,  # type: ignore[arg-type]
            target_executor=FakeTargetExecutor(),  # type: ignore[arg-type]
        )


def test_corpus_rejects_host_x86_contamination(tmp_path: Path) -> None:
    workspace, toolchain = _directories(tmp_path)
    builder = FakeBuildRunner(
        _successful_stdout() + "/usr/lib/x86_64-linux-gnu/libc.so.6\n"
    )

    with pytest.raises(BuildCorpusError, match="contamination"):
        run_same_tree_build_corpus(
            workspace,
            toolchain,
            build_runner=builder,  # type: ignore[arg-type]
            target_executor=FakeTargetExecutor(),  # type: ignore[arg-type]
        )


def test_corpus_wraps_target_marker_failure(tmp_path: Path) -> None:
    workspace, toolchain = _directories(tmp_path)

    with pytest.raises(BuildCorpusError, match="same-tree marker") as captured:
        run_same_tree_build_corpus(
            workspace,
            toolchain,
            build_runner=FakeBuildRunner(_successful_stdout()),  # type: ignore[arg-type]
            target_executor=FakeTargetExecutor(fail=True),  # type: ignore[arg-type]
        )

    assert isinstance(captured.value.__cause__, TargetCommandError)


def test_corpus_wraps_build_failure(tmp_path: Path) -> None:
    workspace, toolchain = _directories(tmp_path)

    with pytest.raises(BuildCorpusError, match="build could not run") as captured:
        run_same_tree_build_corpus(
            workspace,
            toolchain,
            build_runner=FakeBuildRunner("", fail=True),  # type: ignore[arg-type]
            target_executor=FakeTargetExecutor(),  # type: ignore[arg-type]
        )

    assert isinstance(captured.value.__cause__, BuildCommandError)


def test_verify_toolchain_archive_accepts_exact_bytes(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "toolchain.tar"
    archive.write_bytes(b"known toolchain bytes")

    import hashlib
    import orin_stage.build_corpus as module

    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(module, "JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_SHA256", expected)

    assert verify_toolchain_archive(archive) == expected


def test_verify_toolchain_archive_rejects_wrong_bytes(tmp_path: Path) -> None:
    archive = tmp_path / "toolchain.tar"
    archive.write_bytes(b"wrong")

    with pytest.raises(BuildCorpusError, match="SHA-256 mismatch"):
        verify_toolchain_archive(archive)


def test_recorded_toolchain_archive_sha256_is_exact_mvp_identity() -> None:
    assert JP6_BOOTLIN_TOOLCHAIN_ARCHIVE_SHA256 == (
        "8af54f268c462b2d0737df8789b5e35db03a2d1ecbec90e20948f66f9244fcdd"
    )
