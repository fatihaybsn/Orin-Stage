from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orin_stage.base.construction import BaseBuildResult
from orin_stage.catalog import TargetResolver, builtin_catalog_paths
from orin_stage.privileged_base import (
    PrivilegedBaseError,
    ensure_jp623_base_with_sudo,
    main,
    parse_base_build_result,
)


SELECTOR = "jetson-orin@jp6.2.3"


def _target():
    paths = builtin_catalog_paths()
    return TargetResolver(paths.targets_dir, paths.schema_path).resolve(SELECTOR)


def _result(data_root: Path, *, cache_hit: bool = False) -> BaseBuildResult:
    target_directory = data_root.resolve() / "targets" / ("a" * 64)
    return BaseBuildResult(
        target_directory=target_directory,
        base_path=target_directory / "base",
        lock_path=target_directory / "lock.json",
        manifest_path=target_directory / "manifest.json",
        receipt_path=target_directory / "receipt.json",
        target_lock_digest="a" * 64,
        base_digest="b" * 64,
        cache_hit=cache_hit,
    )


def _payload(result: BaseBuildResult) -> str:
    return json.dumps(
        {
            "target_directory": str(result.target_directory),
            "base_path": str(result.base_path),
            "lock_path": str(result.lock_path),
            "manifest_path": str(result.manifest_path),
            "receipt_path": str(result.receipt_path),
            "target_lock_digest": result.target_lock_digest,
            "base_digest": result.base_digest,
            "cache_hit": result.cache_hit,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_sudo_builder_uses_narrow_shell_free_command_once(tmp_path: Path) -> None:
    result = _result(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(command: tuple[str, ...], **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, _payload(result), "")

    receipt = tmp_path / "sdkm" / "receipts" / "receipt.json"
    parsed = ensure_jp623_base_with_sudo(
        _target(),
        acquisition_receipt_path=receipt,
        data_root=tmp_path,
        qemu_binary=Path("/usr/bin/qemu-aarch64-static"),
        runner=runner,
        which=lambda name: "/usr/bin/sudo" if name == "sudo" else None,
    )

    assert parsed == result
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == ("/usr/bin/sudo", "--")
    assert "-E" not in command
    assert "PYTHONPATH" not in command
    assert command[2].startswith("/")
    assert command[3:6] == ("-I", "-m", "orin_stage.privileged_base")
    assert command[command.index("--selector") + 1] == SELECTOR
    assert command[command.index("--data-root") + 1] == str(tmp_path.resolve())
    assert command[command.index("--acquisition-receipt") + 1] == str(
        receipt.resolve()
    )
    assert kwargs == {
        "check": False,
        "capture_output": True,
        "text": True,
        "shell": False,
    }


def test_sudo_builder_preserves_venv_interpreter_symlink(
    monkeypatch,
    tmp_path: Path,
) -> None:
    result = _result(tmp_path)
    commands: list[tuple[str, ...]] = []
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to("/usr/bin/python3.10")
    monkeypatch.setattr(
        "orin_stage.privileged_base.sys.executable",
        str(venv_python),
    )

    def runner(command: tuple[str, ...], **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, _payload(result), "")

    ensure_jp623_base_with_sudo(
        _target(),
        acquisition_receipt_path=tmp_path / "receipt.json",
        data_root=tmp_path,
        qemu_binary=Path("/usr/bin/qemu-aarch64-static"),
        runner=runner,
        which=lambda name: "/usr/bin/sudo" if name == "sudo" else None,
    )

    assert len(commands) == 1
    interpreter = commands[0][2]
    assert interpreter == str(venv_python)
    assert Path(interpreter).is_absolute()
    assert interpreter != str(venv_python.resolve())
    assert commands[0][3:6] == ("-I", "-m", "orin_stage.privileged_base")


def test_sudo_missing_is_short_error_without_subprocess(tmp_path: Path) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("subprocess must not run")

    with pytest.raises(PrivilegedBaseError, match="sudo is not installed"):
        ensure_jp623_base_with_sudo(
            _target(),
            acquisition_receipt_path=tmp_path / "receipt.json",
            data_root=tmp_path,
            qemu_binary=Path("/usr/bin/qemu-aarch64-static"),
            runner=forbidden,
            which=lambda name: None,
        )


def test_sudo_child_failure_is_short_domain_error(tmp_path: Path) -> None:
    def runner(command: tuple[str, ...], **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "error: base validation failed\n",
        )

    with pytest.raises(
        PrivilegedBaseError,
        match="privileged base builder failed: base validation failed",
    ):
        ensure_jp623_base_with_sudo(
            _target(),
            acquisition_receipt_path=tmp_path / "receipt.json",
            data_root=tmp_path,
            qemu_binary=Path("/usr/bin/qemu-aarch64-static"),
            runner=runner,
            which=lambda name: "/usr/bin/sudo" if name == "sudo" else None,
        )


def test_privileged_child_rejects_non_root(capsys, tmp_path: Path) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("builder must not run")

    assert (
        main(
            [
                "--selector",
                SELECTOR,
                "--data-root",
                str(tmp_path),
                "--acquisition-receipt",
                str(tmp_path / "receipt.json"),
                "--qemu",
                "/usr/bin/qemu-aarch64-static",
            ],
            geteuid=lambda: 1000,
            base_builder=forbidden,
        )
        == 1
    )
    assert "must run as root" in capsys.readouterr().err


def test_privileged_child_resolves_exact_inputs_and_writes_json(
    capsys,
    tmp_path: Path,
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    expected = _result(tmp_path)
    receipt = tmp_path / "sdkm" / "receipts" / "receipt.json"

    def builder(target, **kwargs):
        calls.append((target, kwargs))
        return expected

    assert (
        main(
            [
                "--selector",
                SELECTOR,
                "--data-root",
                str(tmp_path),
                "--acquisition-receipt",
                str(receipt),
                "--qemu",
                "/usr/bin/qemu-aarch64-static",
            ],
            geteuid=lambda: 0,
            base_builder=builder,
        )
        == 0
    )
    assert len(calls) == 1
    target, kwargs = calls[0]
    assert target.selector == SELECTOR
    assert kwargs["data_root"] == tmp_path.resolve()
    assert kwargs["acquisition_receipt_path"] == receipt.resolve()
    assert kwargs["qemu_binary"] == Path("/usr/bin/qemu-aarch64-static")
    output = capsys.readouterr().out
    assert parse_base_build_result(output, data_root=tmp_path) == expected


def test_privileged_child_reports_builder_failure_without_traceback(
    capsys,
    tmp_path: Path,
) -> None:
    def builder(*args, **kwargs):
        raise RuntimeError("base construction failed\ninternal details")

    assert (
        main(
            [
                "--selector",
                SELECTOR,
                "--data-root",
                str(tmp_path),
                "--acquisition-receipt",
                str(tmp_path / "receipt.json"),
                "--qemu",
                "/usr/bin/qemu-aarch64-static",
            ],
            geteuid=lambda: 0,
            base_builder=builder,
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: base construction failed\n"
    assert "Traceback" not in captured.err


def test_parent_rejects_malformed_child_json(tmp_path: Path) -> None:
    with pytest.raises(PrivilegedBaseError, match="malformed JSON"):
        parse_base_build_result("{}", data_root=tmp_path)
