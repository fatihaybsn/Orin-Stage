from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import orin_stage.privileged_storage_measure as privileged_measure
from orin_stage.privileged_storage_measure import (
    _measure_target_storage,
    _parse_measurement_result,
    _target_directory,
    main,
    measure_base_storage_with_sudo,
)
from orin_stage.storage import StorageError


TARGET_DIGEST = "a" * 64


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "targets" / TARGET_DIGEST).mkdir(parents=True)
    return root


def test_parent_uses_narrow_shell_free_sudo_command(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"bytes_used":1234}',
            stderr="",
        )

    assert (
        measure_base_storage_with_sudo(
            data_root,
            TARGET_DIGEST,
            runner=runner,
        )
        == 1234
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == ("sudo", "--")
    assert Path(command[2]).is_absolute()
    assert command[3:6] == (
        "-I",
        "-m",
        "orin_stage.privileged_storage_measure",
    )
    assert command[command.index("--data-root") + 1] == str(data_root.resolve())
    assert command[command.index("--target-digest") + 1] == TARGET_DIGEST
    assert str(data_root / "targets" / TARGET_DIGEST) not in command
    assert "-E" not in command
    assert "PYTHONPATH" not in command
    assert kwargs == {
        "check": False,
        "capture_output": True,
        "text": True,
        "shell": False,
    }


def test_parent_preserves_venv_interpreter_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    actual = tmp_path / "python-real"
    actual.write_text("", encoding="utf-8")
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(actual)
    commands: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"bytes_used":0}',
            stderr="",
        )

    monkeypatch.setattr(privileged_measure.sys, "executable", str(interpreter))

    assert (
        measure_base_storage_with_sudo(
            data_root,
            TARGET_DIGEST,
            runner=runner,
        )
        == 0
    )
    assert commands[0][2] == os.path.abspath(interpreter)
    assert commands[0][2] != str(interpreter.resolve())
    assert commands[0][3:6] == (
        "-I",
        "-m",
        "orin_stage.privileged_storage_measure",
    )


@pytest.mark.parametrize("digest", ["short", "A" * 64, "../" + "a" * 61])
def test_parent_rejects_invalid_digest_without_sudo(
    tmp_path: Path,
    digest: str,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("sudo must not run for an invalid digest")

    with pytest.raises(StorageError, match="64 lowercase hexadecimal"):
        measure_base_storage_with_sudo(tmp_path, digest, runner=forbidden)


@pytest.mark.parametrize(
    "payload",
    (
        "not-json",
        "[]",
        "{}",
        '{"bytes_used":true}',
        '{"bytes_used":-1}',
        '{"bytes_used":1,"extra":2}',
    ),
)
def test_parent_rejects_malformed_or_invalid_results(payload: str) -> None:
    with pytest.raises(StorageError, match="privileged storage measurement returned"):
        _parse_measurement_result(payload)


def test_parent_reports_child_failure_as_storage_error(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="error: target validation failed\n",
        )

    with pytest.raises(
        StorageError,
        match="privileged storage measurement failed: target validation failed",
    ):
        measure_base_storage_with_sudo(
            data_root,
            TARGET_DIGEST,
            runner=runner,
        )


def test_child_rejects_non_root_without_measuring(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def forbidden(_path: Path) -> int:
        raise AssertionError("non-root child must not measure")

    monkeypatch.setattr(privileged_measure, "_allocated_tree_bytes", forbidden)

    assert (
        main(
            [
                "--data-root",
                str(tmp_path),
                "--target-digest",
                TARGET_DIGEST,
            ],
            geteuid=lambda: 1000,
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: privileged storage measurement must run as root\n"


def test_child_rejects_target_symlink(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    targets = data_root / "targets"
    outside = tmp_path / "outside"
    targets.mkdir(parents=True)
    outside.mkdir()
    (targets / TARGET_DIGEST).symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageError, match="published base target not found"):
        _measure_target_storage(
            data_root,
            TARGET_DIGEST,
            geteuid=lambda: 0,
        )


def test_child_rejects_targets_symlink(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside-targets"
    data_root.mkdir()
    outside.mkdir()
    (data_root / "targets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageError, match="targets path is not a real directory"):
        _measure_target_storage(
            data_root,
            TARGET_DIGEST,
            geteuid=lambda: 0,
        )


def test_child_digest_cannot_escape_data_root(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)

    with pytest.raises(StorageError, match="64 lowercase hexadecimal"):
        _target_directory(data_root, "../" + "a" * 61)


def test_child_reuses_allocated_tree_algorithm_for_exact_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    calls: list[Path] = []

    def measure(path: Path) -> int:
        calls.append(path)
        return 2468

    monkeypatch.setattr(privileged_measure, "_allocated_tree_bytes", measure)

    assert (
        _measure_target_storage(
            data_root,
            TARGET_DIGEST,
            geteuid=lambda: 0,
        )
        == 2468
    )
    assert calls == [data_root.resolve() / "targets" / TARGET_DIGEST]


def test_child_accounting_deduplicates_hardlinks_ignores_symlink_target_and_counts_seed(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    target = data_root / "targets" / TARGET_DIGEST
    base = target / "base"
    materialization = target / "materialization"
    base.mkdir()
    materialization.mkdir()
    payload = base / "payload"
    payload.write_bytes(b"x" * 8192)
    os.link(payload, base / "hardlink")
    outside = tmp_path / "outside"
    outside.write_bytes(b"y" * 65536)
    symlink = base / "outside-link"
    symlink.symlink_to(outside)
    seed = materialization / "seed.tar"
    seed.write_bytes(b"z" * 4096)

    def allocated(path: Path) -> int:
        stat = path.lstat()
        return stat.st_blocks * 512 if stat.st_blocks else stat.st_size

    expected = sum(
        allocated(path)
        for path in (target, base, payload, symlink, materialization, seed)
    )
    assert (
        _measure_target_storage(
            data_root,
            TARGET_DIGEST,
            geteuid=lambda: 0,
        )
        == expected
    )


def test_root_child_writes_only_deterministic_json(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    target = data_root / "targets" / TARGET_DIGEST
    (target / "materialization").mkdir()
    (target / "materialization" / "seed.tar").write_bytes(b"seed")

    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "--target-digest",
                TARGET_DIGEST,
            ],
            geteuid=lambda: 0,
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert captured.out == json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    assert set(payload) == {"bytes_used"}
    assert isinstance(payload["bytes_used"], int)
