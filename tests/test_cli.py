from __future__ import annotations

import subprocess
import sys
import urllib.request

import pytest

from orin_stage.acquisition.sdk_manager import SdkManagerClient
from orin_stage.cli import PROGRAM_VERSION, _target_exit_code, build_parser, main
from orin_stage.doctor import CheckStatus, DoctorCheck


def test_cli_help_has_product_name_and_data_root(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "Orin Stage" in output
    assert "--data-root PATH" in output
    assert "~/.local/share/orin-stage" in output
    assert "doctor" in output
    assert "target" in output
    assert "workspace" in output
    assert "shell" in output
    assert "run" in output
    assert "build" in output
    assert "inspect" in output
    assert "storage" in output


def test_target_help_contains_list(capsys) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["target", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "list" in output
    assert "ensure" in output


def test_cli_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"ostg {PROGRAM_VERSION}"


def test_cli_module_entrypoint_reports_version() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "orin_stage.cli", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == f"ostg {PROGRAM_VERSION}"


def test_cli_without_subcommand_is_non_destructive_and_shows_help(capsys, tmp_path) -> None:
    data_root = tmp_path / "data"

    assert main(["--data-root", str(data_root)]) == 0
    assert not data_root.exists()
    assert "usage: ostg" in capsys.readouterr().out


def test_cli_doctor_returns_zero_for_healthy_checks(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "orin_stage.cli.run_doctor",
        lambda data_root: [DoctorCheck(CheckStatus.PASS, "Host OS", "Linux")],
    )

    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "Orin Stage Doctor" in output
    assert "PASS" in output
    assert "Summary: 1 PASS, 0 WARN, 0 FAIL" in output


def test_cli_doctor_returns_one_when_a_check_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "orin_stage.cli.run_doctor",
        lambda data_root: [
            DoctorCheck(CheckStatus.FAIL, "Host architecture", "aarch64")
        ],
    )

    assert main(["doctor"]) == 1
    assert "Summary: 0 PASS, 0 WARN, 1 FAIL" in capsys.readouterr().out


def test_cli_keyboard_interrupt_is_short_error_with_exit_130(
    monkeypatch,
    capsys,
) -> None:
    def interrupt(_data_root) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr("orin_stage.cli.run_doctor", interrupt)

    assert main(["doctor"]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: interrupted\n"
    assert "Traceback" not in captured.err


def test_child_signal_exit_code_maps_to_shell_convention() -> None:
    assert _target_exit_code(-15) == 143


def test_target_list_shows_six_ga_targets_in_semantic_order(capsys) -> None:
    assert main(["target", "list"]) == 0

    output = capsys.readouterr().out
    lines = output.strip().splitlines()
    assert lines[0].split() == ["TARGET", "JETPACK", "L4T", "STATUS"]
    rows = [line.split() for line in lines[1:]]
    assert rows == [
        ["jetson-orin@jp6.0", "6.0", "36.3", "validation-pending"],
        ["jetson-orin@jp6.1", "6.1", "36.4", "validation-pending"],
        ["jetson-orin@jp6.2", "6.2", "36.4.3", "validation-pending"],
        ["jetson-orin@jp6.2.1", "6.2.1", "36.4.4", "validation-pending"],
        ["jetson-orin@jp6.2.2", "6.2.2", "36.5.0", "validation-pending"],
        ["jetson-orin@jp6.2.3", "6.2.3", "36.5.2", "validation-pending"],
    ]
    assert "Developer Preview" not in output
    assert "jp6.0-dp" not in output


def test_target_list_does_not_use_network_sdk_manager_doctor_or_data_root(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    def unexpected_call(*args, **kwargs):
        raise AssertionError("unexpected external call")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_call)
    monkeypatch.setattr(SdkManagerClient, "version", unexpected_call)
    monkeypatch.setattr(SdkManagerClient, "query_jetson", unexpected_call)
    monkeypatch.setattr("orin_stage.cli.run_doctor", unexpected_call)
    data_root = tmp_path / "data-root"

    assert main(["--data-root", str(data_root), "target", "list"]) == 0
    assert not data_root.exists()
    assert "jetson-orin@jp6.2.3" in capsys.readouterr().out


def test_target_list_reports_catalog_errors_without_traceback(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    from orin_stage.catalog import BuiltinCatalogPaths

    missing = tmp_path / "missing"
    monkeypatch.setattr(
        "orin_stage.cli.builtin_catalog_paths",
        lambda: BuiltinCatalogPaths(
            targets_dir=missing / "targets",
            schema_path=missing / "target.schema.json",
            hardware_dir=missing / "hardware",
        ),
    )

    assert main(["target", "list"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: cannot load built-in target catalog:")
    assert "Traceback" not in captured.err
