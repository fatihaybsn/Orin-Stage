from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orin_stage.materialization_seed import MaterializationSeedResult
from orin_stage.privileged_materialization import (
    create_materialization_seed_with_sudo,
    main,
    parse_materialization_seed_result,
)


def _target(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    target = data_root / "targets" / ("a" * 64)
    target.mkdir(parents=True)
    return data_root, target


def _result(target: Path) -> MaterializationSeedResult:
    return MaterializationSeedResult(
        archive_path=target / "materialization" / "seed.tar",
        metadata_path=target / "materialization" / "seed.json",
        seed_sha256="b" * 64,
    )


def _payload(result: MaterializationSeedResult) -> str:
    return json.dumps(
        {
            "archive_path": str(result.archive_path),
            "metadata_path": str(result.metadata_path),
            "seed_sha256": result.seed_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_sudo_seed_builder_preserves_venv_interpreter_symlink(
    monkeypatch,
    tmp_path: Path,
) -> None:
    data_root, target = _target(tmp_path)
    expected = _result(target)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to("/usr/bin/python3.10")
    monkeypatch.setattr(
        "orin_stage.privileged_materialization.sys.executable",
        str(venv_python),
    )

    def runner(command: tuple[str, ...], **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, _payload(expected), "")

    result = create_materialization_seed_with_sudo(
        target,
        data_root=data_root,
        runner=runner,
        which=lambda name: "/usr/bin/sudo" if name == "sudo" else None,
    )

    assert result == expected
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == ("/usr/bin/sudo", "--")
    assert command[2] == str(venv_python)
    assert command[2] != str(venv_python.resolve())
    assert command[3:5] == ("-m", "orin_stage.privileged_materialization")
    assert "-E" not in command
    assert command[command.index("--data-root") + 1] == str(data_root.resolve())
    assert command[command.index("--target-dir") + 1] == str(target.resolve())
    assert kwargs == {
        "check": False,
        "capture_output": True,
        "text": True,
        "shell": False,
    }


def test_privileged_seed_child_rejects_non_root(capsys, tmp_path: Path) -> None:
    data_root, target = _target(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("seed creator must not run")

    assert (
        main(
            ["--data-root", str(data_root), "--target-dir", str(target)],
            geteuid=lambda: 1000,
            seed_creator=forbidden,
        )
        == 1
    )
    assert "must run as root" in capsys.readouterr().err


def test_privileged_seed_child_uses_exact_target_and_returns_json(
    capsys,
    tmp_path: Path,
) -> None:
    data_root, target = _target(tmp_path)
    expected = _result(target)
    calls: list[Path] = []

    def creator(selected: Path) -> MaterializationSeedResult:
        calls.append(selected)
        return expected

    assert (
        main(
            ["--data-root", str(data_root), "--target-dir", str(target)],
            geteuid=lambda: 0,
            seed_creator=creator,
        )
        == 0
    )
    assert calls == [target.resolve()]
    output = capsys.readouterr().out
    assert parse_materialization_seed_result(output, target_dir=target) == expected


def test_privileged_seed_child_rejects_target_outside_data_root(
    capsys,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert (
        main(
            ["--data-root", str(data_root), "--target-dir", str(outside)],
            geteuid=lambda: 0,
        )
        == 1
    )
    assert "is not under" in capsys.readouterr().err
