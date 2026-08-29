from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import orin_stage.privileged_storage_delete as privileged_delete
from orin_stage.privileged_storage_delete import (
    _parse_deletion_result,
    _remove_target_storage,
    _target_directory,
    main,
    remove_base_storage_with_sudo,
)
from orin_stage.storage import StorageError


TARGET_DIGEST = "a" * 64
SIBLING_DIGEST = "b" * 64


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    target = root / "targets" / TARGET_DIGEST
    (target / "base" / "nested").mkdir(parents=True)
    (target / "base" / "nested" / "payload").write_text("base", encoding="utf-8")
    (target / "materialization").mkdir()
    (target / "materialization" / "seed.tar").write_bytes(b"seed")
    (target / "lock.json").write_text("{}", encoding="utf-8")
    return root


def _success(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=json.dumps(
            {"removed": True, "target_lock_digest": TARGET_DIGEST}
        ),
        stderr="",
    )


def test_parent_uses_narrow_shell_free_sudo_command(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return _success(command)

    remove_base_storage_with_sudo(data_root, TARGET_DIGEST, runner=runner)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == ("sudo", "--")
    assert Path(command[2]).is_absolute()
    assert command[3:5] == ("-m", "orin_stage.privileged_storage_delete")
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
        return _success(command)

    monkeypatch.setattr(privileged_delete.sys, "executable", str(interpreter))

    remove_base_storage_with_sudo(data_root, TARGET_DIGEST, runner=runner)

    assert commands[0][2] == os.path.abspath(interpreter)
    assert commands[0][2] != str(interpreter.resolve())


@pytest.mark.parametrize("digest", ("short", "A" * 64, "../" + "a" * 61))
def test_parent_rejects_invalid_digest_without_sudo(
    tmp_path: Path,
    digest: str,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("sudo must not run")

    with pytest.raises(StorageError, match="64 lowercase hexadecimal"):
        remove_base_storage_with_sudo(tmp_path, digest, runner=forbidden)


@pytest.mark.parametrize(
    "payload",
    (
        "not-json",
        "[]",
        "{}",
        '{"removed":false,"target_lock_digest":"' + TARGET_DIGEST + '"}',
        '{"removed":true,"target_lock_digest":"' + SIBLING_DIGEST + '"}',
        '{"removed":true,"target_lock_digest":"' + TARGET_DIGEST + '","extra":1}',
    ),
)
def test_parent_rejects_malformed_or_inconsistent_results(payload: str) -> None:
    with pytest.raises(StorageError, match="privileged storage deletion returned"):
        _parse_deletion_result(payload, expected_digest=TARGET_DIGEST)


def test_parent_reports_child_failure_without_traceback_details(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)

    def runner(
        command: tuple[str, ...],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Traceback (most recent call last):\nRuntimeError: secret detail\n",
        )

    with pytest.raises(StorageError, match="failed: exit 1") as captured:
        remove_base_storage_with_sudo(data_root, TARGET_DIGEST, runner=runner)

    assert "Traceback" not in str(captured.value)
    assert "secret detail" not in str(captured.value)


def test_child_rejects_non_root_without_deleting(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)

    def forbidden(_path: Path) -> None:
        raise AssertionError("non-root child must not delete")

    with pytest.raises(StorageError, match="must run as root"):
        _remove_target_storage(
            data_root,
            TARGET_DIGEST,
            geteuid=lambda: 1000,
            remover=forbidden,
        )

    assert (data_root / "targets" / TARGET_DIGEST).is_dir()


def test_child_rejects_targets_symlink(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside-targets"
    data_root.mkdir()
    outside.mkdir()
    (outside / TARGET_DIGEST).mkdir()
    (data_root / "targets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageError, match="targets path is not a real directory"):
        _target_directory(data_root, TARGET_DIGEST)


def test_child_rejects_target_symlink(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    targets = data_root / "targets"
    outside = tmp_path / "outside-target"
    targets.mkdir(parents=True)
    outside.mkdir()
    (targets / TARGET_DIGEST).symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageError, match="published base target not found"):
        _target_directory(data_root, TARGET_DIGEST)


def test_child_rejects_data_root_symlink(tmp_path: Path) -> None:
    real_root = _data_root(tmp_path)
    linked_root = tmp_path / "linked-data"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(StorageError, match="data root is not a real directory"):
        _target_directory(linked_root, TARGET_DIGEST)


def test_child_digest_cannot_traverse_outside_data_root(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)

    with pytest.raises(StorageError, match="64 lowercase hexadecimal"):
        _target_directory(data_root, "../" + "a" * 61)


def test_child_rejects_missing_target(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)

    with pytest.raises(StorageError, match="published base target not found"):
        _target_directory(data_root, SIBLING_DIGEST)


def test_successful_child_removes_exact_target_tree_and_keeps_sibling(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    target = data_root / "targets" / TARGET_DIGEST
    sibling = data_root / "targets" / SIBLING_DIGEST
    (sibling / "base").mkdir(parents=True)
    (sibling / "base" / "keep").write_text("keep", encoding="utf-8")

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
    assert json.loads(captured.out) == {
        "removed": True,
        "target_lock_digest": TARGET_DIGEST,
    }
    assert not target.exists()
    assert (sibling / "base" / "keep").is_file()
    assert data_root.is_dir()
